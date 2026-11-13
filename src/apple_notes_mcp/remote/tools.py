"""Remote tool allowlist and annotations (issue #167, amendment 1 + brief §5).

Every stdio tool sits in exactly one category; a new tool that isn't
categorized here makes the remote server refuse to start (and fails
tests/test_remote_tools.py), so nothing reaches the remote surface by default.

- READ_TOOLS: exposed remotely by default.
- WRITE_TOOLS: only with an explicit ``write_tools`` opt-in in remote.json.
  Most drive Notes.app by UI scripting (keystrokes + clipboard, needs the
  screen unlocked); delete_note and move_note are pure AppleScript, behind
  exact-one-note guards (#229, #230) - see docs/remote-connector.md before
  enabling any. Remotely they are queued copies (write_tools.py, issue #169):
  the call is accepted into the write queue and applied later through the
  stdio pipeline.
- REMOTE_ONLY_TOOLS: tools with no stdio counterpart, present only when write
  tools are on (get_write_status).
- NEVER_REMOTE: export_* write files to a caller-chosen path (a prompt-injected
  agent could plant a CLAUDE.md or slash command). They cannot be opted in.
  (delete_note was here until #229: it is now an opt-in queued write that
  only ever moves one exactly-named note to Recently Deleted.)

Annotations are applied to the remote copies only, so the stdio tools/list is
byte-for-byte what it was.
"""

import functools
from typing import Iterable, Sequence

import anyio

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.tools import Tool
from mcp.types import ToolAnnotations

READ_TOOLS = frozenset(
    {
        "list_notes",
        "list_folders",
        "read_note_content",
        "get_backlinks",
        "search_notes",
        "get_note_graph",
        "find_orphans",
        "find_broken_links",
        "note_health_check",
        "suggest_links",
    }
)

# Apple Reminders (issue #171): a separate source, merged only when remote.json
# enables it. Kept apart from READ_TOOLS, which mirrors the stdio server.
REMINDERS_READ_TOOLS = frozenset({"reminders_lists", "reminders_list"})
# Opt-in through reminders.write_tools in remote.json (Phase 2), never the
# top-level write_tools.
REMINDERS_WRITE_TOOLS = frozenset(
    {"reminders_complete", "reminders_create", "reminders_update"}
)

WRITE_TOOLS = frozenset(
    {
        "add_note",
        "update_note_content",
        "toggle_checklist_item",
        "replace_note_link",
        "edit_note_section",
        "delete_note",
        "move_note",
    }
)

REMOTE_ONLY_TOOLS = frozenset({"get_write_status"})

NEVER_REMOTE = frozenset({"export_note", "export_folder", "export_graph"})

# Claude rejects tool results over roughly 150k characters.
MAX_RESULT_CHARS = 150_000

_READ_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
)
_WRITE_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False
)
# Moving a note changes where it lives, not what it says (#230); moving it
# twice to the same folder is a no-op.
_MOVE_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False
)
# Adding a reminder changes nothing that exists (design doc, Q2).
_CREATE_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False
)


def validate_write_tools(write_tools: Iterable[str]) -> tuple[str, ...]:
    """Return the opt-in write tools, refusing never-remote and unknown names."""
    names = tuple(write_tools)
    for name in names:
        if name in NEVER_REMOTE:
            raise ValueError(f"{name} can never be exposed remotely")
        if name in REMINDERS_WRITE_TOOLS:
            raise ValueError(f"{name} goes in reminders.write_tools, not write_tools")
        if name not in WRITE_TOOLS:
            raise ValueError(
                f"{name} is not an opt-in write tool (choose from {sorted(WRITE_TOOLS)})"
            )
    return names


def validate_reminders_write_tools(write_tools: Iterable[str]) -> tuple[str, ...]:
    """Return the opt-in Reminders write tools, refusing unknown names."""
    names = tuple(write_tools)
    for name in names:
        if name not in REMINDERS_WRITE_TOOLS:
            raise ValueError(
                f"{name} is not a Reminders write tool (choose from {sorted(REMINDERS_WRITE_TOOLS)})"
            )
    return names


def threaded(tool: Tool) -> Tool:
    """A copy of a sync tool that runs in a worker thread.

    The stdio tools are plain ``def``s and FastMCP calls those directly on the
    event loop, so one slow read (a content search, a big graph) would stall
    every other request - token refreshes and sign-in callbacks included.
    """
    if tool.is_async:
        return tool
    fn = tool.fn

    async def run_in_thread(**kwargs):
        return await anyio.to_thread.run_sync(functools.partial(fn, **kwargs))

    return tool.model_copy(update={"fn": run_in_thread, "is_async": True})


def remote_tools(
    source: FastMCP,
    write_tools: Iterable[str] = (),
    extra_sources: Sequence[FastMCP] = (),
    reminders_write_tools: Iterable[str] = (),
    *,
    applier=None,
    worker=None,
) -> list[Tool]:
    """Annotated copies of the sources' tools allowed in remote mode.

    ``extra_sources`` (the Reminders tools, issue #171) go through the same
    categorization check as the stdio server's tools; a Reminders write tool is
    exposed only when ``reminders_write_tools`` lists it, and runs directly
    (EventKit, not queued). Opted-in Notes write tools become queued copies
    backed by ``applier`` (a write_apply.WriteApplier, issue #169); building
    them without one is refused, so a remote write can never be accepted into
    memory and lost.
    """
    from .write_tools import queued_write_tool, write_status_tool

    enabled_writes = set(validate_write_tools(write_tools))
    if enabled_writes and applier is None:
        raise ValueError("write tools need a write queue (applier) in remote mode")
    reads = READ_TOOLS | REMINDERS_READ_TOOLS
    allowed = (
        reads
        | enabled_writes
        | set(validate_reminders_write_tools(reminders_write_tools))
    )
    known = reads | WRITE_TOOLS | REMINDERS_WRITE_TOOLS | NEVER_REMOTE
    tools = [t for src in (source, *extra_sources) for t in src._tool_manager.list_tools()]
    seen: set[str] = set()
    for tool in tools:
        if tool.name in seen:
            raise ValueError(f"tool {tool.name} is defined by more than one source")
        seen.add(tool.name)
    copies = []
    for tool in tools:
        if tool.name not in known:
            raise ValueError(
                f"tool {tool.name} is not categorized in apple_notes_mcp/remote/tools.py"
            )
        if tool.name not in allowed:
            continue
        if tool.name in reads:
            copies.append(threaded(tool.model_copy(update={"annotations": _READ_ANNOTATIONS})))
        elif tool.name in REMINDERS_WRITE_TOOLS:
            annotations = (
                _CREATE_ANNOTATIONS if tool.name == "reminders_create" else _WRITE_ANNOTATIONS
            )
            copies.append(threaded(tool.model_copy(update={"annotations": annotations})))
        else:
            annotations = (
                _MOVE_ANNOTATIONS if tool.name == "move_note" else _WRITE_ANNOTATIONS
            )
            copies.append(queued_write_tool(tool, applier, worker, annotations))
    if enabled_writes:
        copies.append(threaded(write_status_tool(applier.queue)))
    return copies
