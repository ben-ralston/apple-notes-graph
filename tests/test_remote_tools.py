"""Remote-mode tool allowlist + annotations (issue #167, amendment 1 + brief §5).

The remote server exposes copies of the stdio server's tools: read tools by
default, write tools only by explicit opt-in (delete_note and move_note
included, #229/#230), and export_* never.
"""

import asyncio

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from apple_notes_mcp import server
from apple_notes_mcp.remote import tools as remote_tools


@pytest.fixture
def stub_applier(tmp_path):
    """Write tools are queued remotely (#169); building them needs a queue."""
    from types import SimpleNamespace

    from apple_notes_mcp.remote.write_queue import WriteQueue, WriteQueueConfig

    return SimpleNamespace(queue=WriteQueue(WriteQueueConfig(state_dir=tmp_path / "wq")))


def _stdio_tool_names():
    return {t.name for t in server.mcp._tool_manager.list_tools()}


def test_every_stdio_tool_is_categorized_exactly_once():
    cats = [
        remote_tools.READ_TOOLS,
        remote_tools.WRITE_TOOLS,
        remote_tools.NEVER_REMOTE,
    ]
    union = set().union(*cats)
    assert union == _stdio_tool_names()
    assert sum(len(c) for c in cats) == len(union)


def test_exports_are_never_remote():
    assert remote_tools.NEVER_REMOTE == {
        "export_note",
        "export_folder",
        "export_graph",
    }


@pytest.mark.parametrize("name", ["delete_note", "move_note"])
def test_delete_and_move_are_queued_opt_in_write_tools(name):
    """#229/#230: remote only by write_tools opt-in, through the queue."""
    from apple_notes_mcp.remote.write_queue import ALL_OPERATIONS

    assert name in remote_tools.WRITE_TOOLS
    assert name in ALL_OPERATIONS
    assert name not in remote_tools.NEVER_REMOTE


def test_replace_note_link_is_a_queued_write_tool():
    """#183: the relink tool is remote only by write_tools opt-in, via the queue."""
    from apple_notes_mcp.remote.write_queue import ALL_OPERATIONS

    assert "replace_note_link" in remote_tools.WRITE_TOOLS
    assert "replace_note_link" in ALL_OPERATIONS


def test_edit_note_section_is_a_remote_write_tool():
    """#184: categorized as a write tool (remote only by write_tools opt-in)."""
    assert "edit_note_section" in remote_tools.WRITE_TOOLS
    assert "edit_note_section" not in remote_tools.READ_TOOLS


def test_default_remote_tools_are_read_only():
    names = {t.name for t in remote_tools.remote_tools(server.mcp)}
    assert names == remote_tools.READ_TOOLS
    assert not names & remote_tools.WRITE_TOOLS


def test_write_tools_need_explicit_opt_in(stub_applier):
    names = {
        t.name
        for t in remote_tools.remote_tools(
            server.mcp, write_tools=["add_note"], applier=stub_applier
        )
    }
    assert names == remote_tools.READ_TOOLS | {"add_note"} | remote_tools.REMOTE_ONLY_TOOLS


@pytest.mark.parametrize("name", sorted(remote_tools.NEVER_REMOTE | {"no_such_tool"}))
def test_never_remote_or_unknown_tools_cannot_be_opted_in(name):
    with pytest.raises(ValueError):
        remote_tools.remote_tools(server.mcp, write_tools=[name])


def test_uncategorized_source_tool_is_refused():
    from mcp.server.fastmcp import FastMCP

    other = FastMCP("x")

    @other.tool()
    def brand_new_tool() -> str:
        return "x"

    with pytest.raises(ValueError, match="brand_new_tool"):
        remote_tools.remote_tools(other)


def test_annotations_mark_reads_read_only_and_writes_destructive(stub_applier):
    tools = {
        t.name: t
        for t in remote_tools.remote_tools(
            server.mcp, write_tools=sorted(remote_tools.WRITE_TOOLS), applier=stub_applier
        )
    }
    for name in remote_tools.READ_TOOLS | remote_tools.REMOTE_ONLY_TOOLS:
        ann = tools[name].annotations
        assert ann.readOnlyHint is True and ann.destructiveHint is False, name
    for name in remote_tools.WRITE_TOOLS - {"move_note"}:
        ann = tools[name].annotations
        assert ann.readOnlyHint is False and ann.destructiveHint is True, name
    # A move changes where a note lives, not what it says (#230).
    ann = tools["move_note"].annotations
    assert ann.readOnlyHint is False and ann.destructiveHint is False
    assert ann.idempotentHint is True


def test_building_remote_tools_does_not_touch_stdio_tools(stub_applier):
    def snapshot():
        return {
            t.name: (t.annotations, t.fn, t.description, t.is_async)
            for t in server.mcp._tool_manager.list_tools()
        }

    before = snapshot()
    remote_tools.remote_tools(
        server.mcp, write_tools=sorted(remote_tools.WRITE_TOOLS), applier=stub_applier
    )
    after = {t.name: t.annotations for t in server.mcp._tool_manager.list_tools()}
    assert snapshot() == before
    assert all(a is None for a in after.values())


def test_oversized_results_are_refused_not_sent():
    from apple_notes_mcp.remote.app import RemoteFastMCP
    from apple_notes_mcp.remote.request_log import RequestLog

    remote = RemoteFastMCP("t", request_log=RequestLog(None))

    @remote.tool()
    def huge() -> str:
        return "x" * (remote_tools.MAX_RESULT_CHARS + 1)

    @remote.tool()
    def small() -> str:
        return "ok"

    with pytest.raises(ToolError, match="too large"):
        asyncio.run(remote.call_tool("huge", {}))
    assert asyncio.run(remote.call_tool("small", {}))


def test_remote_tools_run_off_the_event_loop():
    import time

    from mcp.server.fastmcp import FastMCP

    src = FastMCP("x")

    @src.tool()
    def slow() -> str:
        time.sleep(0.3)
        return "ok"

    remote = FastMCP("r", tools=[remote_tools.threaded(src._tool_manager.get_tool("slow"))])

    async def both():
        start = time.monotonic()
        await asyncio.gather(remote.call_tool("slow", {}), remote.call_tool("slow", {}))
        return time.monotonic() - start

    assert asyncio.run(both()) < 0.55
    assert all(t.is_async for t in remote_tools.remote_tools(server.mcp))


def test_reminders_tools_are_categorized_read():
    from apple_notes_mcp.reminders.server import build_reminders_mcp
    from reminders_fakes import FakeStore

    reminders = build_reminders_mcp(FakeStore())
    copies = remote_tools.remote_tools(server.mcp, extra_sources=[reminders])
    by_name = {t.name: t for t in copies}
    for name in ("reminders_lists", "reminders_list"):
        assert name in remote_tools.REMINDERS_READ_TOOLS
        assert by_name[name].annotations.readOnlyHint is True


def test_duplicate_tool_across_sources_is_refused():
    from mcp.server.fastmcp import FastMCP

    other = FastMCP("dup")

    @other.tool()
    def list_notes() -> str:
        return "shadow"

    with pytest.raises(ValueError, match="defined by more than one source"):
        remote_tools.remote_tools(server.mcp, extra_sources=[other])


def test_stdio_server_has_no_reminders_tools():
    assert not [n for n in _stdio_tool_names() if n.startswith("reminders_")]


def _reminders_source():
    from apple_notes_mcp.reminders.server import build_reminders_mcp
    from reminders_fakes import FakeStore

    return build_reminders_mcp(FakeStore())


def test_reminders_writes_hidden_unless_opted_in():
    copies = remote_tools.remote_tools(server.mcp, extra_sources=[_reminders_source()])
    names = {t.name for t in copies}
    assert names == remote_tools.READ_TOOLS | remote_tools.REMINDERS_READ_TOOLS
    assert len(names) == 12


def test_reminders_write_annotations():
    copies = remote_tools.remote_tools(
        server.mcp,
        extra_sources=[_reminders_source()],
        reminders_write_tools=("reminders_complete", "reminders_create", "reminders_update"),
    )
    by_name = {t.name: t.annotations for t in copies}
    update = by_name["reminders_update"]
    assert update.readOnlyHint is False and update.destructiveHint is True
    complete, create = by_name["reminders_complete"], by_name["reminders_create"]
    assert complete.readOnlyHint is False and complete.destructiveHint is True
    assert create.readOnlyHint is False and create.destructiveHint is False
    assert create.idempotentHint is False and create.openWorldHint is False
    assert "add_note" not in by_name


def test_reminders_write_opt_in_is_validated():
    with pytest.raises(ValueError, match="not a Reminders write tool"):
        remote_tools.remote_tools(
            server.mcp, extra_sources=[_reminders_source()], reminders_write_tools=["add_note"]
        )


def test_notes_writes_queue_while_reminders_writes_stay_direct(stub_applier):
    """#169 + #171: both opt-ins at once. Notes writes are queued copies;
    Reminders writes are plain threaded EventKit calls, never queued."""
    from apple_notes_mcp.remote.write_tools import QUEUED_NOTE

    copies = remote_tools.remote_tools(
        server.mcp,
        sorted(remote_tools.WRITE_TOOLS),
        [_reminders_source()],
        ("reminders_complete", "reminders_create"),
        applier=stub_applier,
    )
    by_name = {t.name: t for t in copies}
    for name in remote_tools.WRITE_TOOLS:
        assert QUEUED_NOTE in by_name[name].description
    for name in ("reminders_complete", "reminders_create"):
        assert QUEUED_NOTE not in by_name[name].description
    assert [t.name for t in copies].count("get_write_status") == 1


def test_reminders_writes_alone_need_no_write_queue():
    copies = remote_tools.remote_tools(
        server.mcp, (), [_reminders_source()], ("reminders_complete",)
    )
    names = {t.name for t in copies}
    assert "reminders_complete" in names and "get_write_status" not in names
