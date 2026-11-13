"""Queued remote copies of the write tools + get_write_status (issue #169).

A remote write tool is a copy of the stdio tool (same name, same input schema,
same argument validation) whose function queues the call instead of typing
into Notes. It returns at once - no waiting on the UI write, so a client
timeout can never make Claude resubmit a duplicate. Duet follows up with the
remote-only read tool ``get_write_status``.
"""

from datetime import datetime, timezone
from typing import Optional

import anyio
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.fastmcp.tools import Tool
from mcp.types import ToolAnnotations

from .write_apply import WriteRefused

STATUS_LIMIT = 20

QUEUED_NOTE = (
    "\n\nRemote: this write is QUEUED, not done. The call returns at once with "
    "{status, write_id, applies}; the change lands in the note through the same "
    "pipeline as a local write - right away if the Mac is unlocked, otherwise about "
    "1 minute after it is next unlocked. Don't resubmit it; call "
    "get_write_status(write_id) later to see whether it was applied, applied only in "
    "part (applied_unverified: check the note, don't resubmit), hit a conflict "
    "(the note changed in between: re-read it and decide again) or failed."
)

_STATUS_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
)


def _iso(ts: Optional[float]) -> Optional[str]:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


def queued_write_tool(
    stdio_tool: Tool, applier, worker, annotations: ToolAnnotations
) -> Tool:
    """A copy of ``stdio_tool`` that queues the call through ``applier``."""
    name = stdio_tool.name

    async def queue_write(**kwargs):
        token = get_access_token()  # on the event loop, before leaving it
        subject = token.subject if token else None

        def submit() -> dict:
            try:
                accepted = applier.accept(name, kwargs, subject=subject)
            except WriteRefused as e:
                raise ToolError(str(e)) from None
            if worker is not None:
                worker.wake()
                applies = worker.applies_hint()
            else:
                applies = "when the owner runs ./run remote-writes apply"
            out = {
                "status": "queued",
                "write_id": accepted["write_id"],
                "queued_ahead": accepted["queued_ahead"],
                "applies": applies,
                "next": "call get_write_status(write_id) to check the outcome",
            }
            if "note" in accepted:
                # delete_note / move_note (#229, #230): which note the queued
                # write resolved to, so the caller can confirm it now.
                out["note"] = accepted["note"]
            return out

        return await anyio.to_thread.run_sync(submit)

    return stdio_tool.model_copy(
        update={
            "fn": queue_write,
            "is_async": True,
            "description": stdio_tool.description.rstrip() + QUEUED_NOTE,
            "annotations": annotations,
        }
    )


def _public(rec: dict) -> dict:
    """What a remote caller may see about a write: never args or contents."""
    return {
        "write_id": rec["id"],
        "tool": rec["tool"],
        "operation": rec["operation"],
        "note_title": rec["note_title"],
        "status": rec["status"],
        "reason": rec["reason"],
        "queued_at": _iso(rec["created_at"]),
        "applied_at": _iso(rec["applied_at"]),
    }


def write_status_tool(queue) -> Tool:
    def get_write_status(write_id: Optional[str] = None) -> dict:
        """Status of queued remote Notes writes.

        With write_id: that write. Without: the most recent 20. Statuses:
        queued (waiting for the Mac to be unlocked), applying, applied,
        applied_unverified (the note changed, but part of the write isn't
        confirmed in it - the reason says what; check the note, don't
        resubmit), conflict (the note changed after the write was queued -
        re-read it and decide again), failed, expired (not applied within
        48 h), rejected (cleared by the owner), undone. Never returns note contents.
        """
        if write_id:
            rec = queue.get(write_id)
            if rec is None:
                raise ToolError(f"no write {write_id!r}")
            return _public(rec)
        return {"writes": [_public(r) for r in queue.list(limit=STATUS_LIMIT)]}

    return Tool.from_function(get_write_status, annotations=_STATUS_ANNOTATIONS)
