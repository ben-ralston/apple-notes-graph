"""``apple-notes-mcp-remote``: serve remote mode, revoke tokens, check config (#167)."""

import argparse
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, Optional

# grant.py has no EventKit import: revoke-all / allow-client / serve must not need it.
from apple_notes import demo_mode
from apple_notes_mcp.reminders.grant import grant_instructions, wrapper_reminders_status

from .config import RemoteConfigError, load_remote_config


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="apple-notes-mcp-remote",
        description="Apple Notes MCP over Streamable HTTP + OAuth on 127.0.0.1 (see docs/remote-connector.md).",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="remote.json path (default: notes-graph config dir)",
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("serve", help="run the HTTP server (default)")
    sub.add_parser(
        "revoke-all", help="wipe every issued token, pin and in-flight sign-in"
    )
    allow = sub.add_parser(
        "allow-client", help="allow another connector client_id (trust-on-first-use pin)"
    )
    allow.add_argument("client_id")
    sub.add_parser(
        "check-config",
        help="validate remote.json + secret and print what would be exposed",
    )
    writes = sub.add_parser("writes", help="inspect and manage the remote write queue (#169)")
    wsub = writes.add_subparsers(dest="writes_command", required=True)
    wl = wsub.add_parser("list", help="recent queued writes (no contents)")
    wl.add_argument("--all", action="store_true", help="every record, not just the last 20")
    wa = wsub.add_parser("apply", help="apply queued writes now (Mac must be unlocked)")
    wa.add_argument("write_id", nargs="?")
    wa.add_argument("--all", action="store_true", help="every queued write, oldest first")
    wu = wsub.add_parser(
        "undo", help="put back what an applied (or applied_unverified) write replaced"
    )
    wu.add_argument("write_id")
    wsub.add_parser("pause", help="stop the server applying queued writes (still accepts)")
    wsub.add_parser("resume", help="let the server apply queued writes again")
    wsub.add_parser("clear", help="reject every queued write")
    parser.set_defaults(command="serve")
    return parser


def _print_reminders_writes(reminders) -> None:
    """The Reminders write scope, by list name only (no reminder contents)."""
    pad = " " * 17
    writes = reminders.write_tools
    print(f"{pad}writes: {', '.join(writes) if writes else 'none (read-only)'}")
    if "reminders_complete" in writes:
        blocked = ", ".join(reminders.never_complete_lists) or "(none)"
        print(f"{pad}never complete: {blocked}")
    where = reminders.create_lists
    where = ", ".join(where) if where is not None else "every list"
    if "reminders_create" in writes:
        print(f"{pad}create in: {where}")
    if "reminders_update" in writes:
        # A moved reminder may land only where a new one could (#232).
        print(f"{pad}move into: {where}")


def _check_config(
    config_path: Optional[Path],
    reminders_status: Callable[[], str] = wrapper_reminders_status,
) -> int:
    from apple_notes_mcp import server as stdio_server

    from .tools import remote_tools

    from .write_queue import WriteQueue, WriteQueueConfig

    cfg = load_remote_config(config_path)
    extra_sources = []
    if cfg.reminders.enabled:
        from apple_notes_mcp.reminders.server import build_reminders_mcp
        from apple_notes_mcp.reminders.store import EventKitStore

        extra_sources.append(build_reminders_mcp(EventKitStore()))
    with tempfile.TemporaryDirectory() as scratch:
        # A throwaway queue: listing the tools must not create the real one.
        applier = SimpleNamespace(queue=WriteQueue(WriteQueueConfig(state_dir=Path(scratch))))
        tools = sorted(
            t.name
            for t in remote_tools(
                stdio_server.mcp,
                cfg.write_tools,
                extra_sources,
                cfg.reminders.write_tools,
                applier=applier if cfg.write_tools else None,
            )
        )
    print(f"public URL:      {cfg.mcp_url}")
    print(f"listening on:    http://{cfg.host}:{cfg.port}/mcp")
    print(
        f"allowed GitHub:  {cfg.allowed_github_login} (id {cfg.allowed_github_id or 'not pinned'})"
    )
    print(
        f"folders:         {', '.join(cfg.folder_allowlist) if cfg.folder_allowlist else 'all'}"
    )
    if cfg.reminders.enabled:
        status = reminders_status()
        print(f"reminders:       on, Apple Notes MCP.app access: {status}")
        if status != "granted":
            print(f"                 {grant_instructions(status)}")
        _print_reminders_writes(cfg.reminders)
    else:
        print('reminders:       off (set "reminders": {"enabled": true} in remote.json)')
    print(f"tools ({len(tools)}):      {', '.join(tools)}")
    wq = cfg.write_queue
    if cfg.write_tools:
        mode = (
            f"auto-apply on, {wq.apply_delay_after_unlock_seconds} s after unlock"
            if wq.auto_apply
            else "auto-apply off (./run remote-writes apply)"
        )
        print(f"write queue:     {wq.state_dir} ({mode}; ops: {', '.join(sorted(wq.allowed_operations))})")
    else:
        print("write queue:     off (no write_tools)")
    print("client secret:   found")
    return 0


def _serve(config_path: Optional[Path]) -> int:
    import uvicorn

    from .app import build_remote_app

    from apple_notes import unified_log

    from . import logger_events, request_log

    # Unattended under launchd (#170): faults go to the unified logger, with
    # exception text and note titles kept out of every event.
    unified_log.configure(component="remote", redact=True)
    request_log.configure_remote_logging()
    try:
        cfg = load_remote_config(config_path)
    except Exception as e:  # RemoteConfigError, or a malformed file's TypeError
        logger_events.report_config_failure(e)
        raise
    phase = "build"
    try:
        app = build_remote_app(cfg)
        worker = getattr(app, "write_worker", None)
        if worker is not None:
            worker.start()
            print(f"apple-notes-mcp-remote: write queue draining from {cfg.write_queue.state_dir}", file=sys.stderr)
        print(
            f"apple-notes-mcp-remote: serving http://{cfg.host}:{cfg.port}/mcp for {cfg.mcp_url}",
            file=sys.stderr,
        )
        phase = "serve"
        uvicorn.run(
            app,
            host=cfg.host,
            port=cfg.port,
            log_level="warning",
            access_log=False,
            # The tunnel connects from 127.0.0.1; never let X-Forwarded-* rewrite
            # the client/scheme (issuer and callback URLs come from config anyway).
            proxy_headers=False,
            forwarded_allow_ips="",
            server_header=False,
            log_config=None,  # keep uvicorn on the redacting root handler
        )
    except BaseException as e:
        logger_events.report_server_failure(phase, e)
        raise
    return 0


def _make_applier(queue):
    from .write_apply import WriteApplier

    return WriteApplier(queue)


def _session_available() -> bool:
    from apple_notes import session_lock

    return session_lock.ui_session_available()


def _restore_frontmost():
    """Capture the frontmost app now; the returned callable re-activates it."""
    from .write_worker import frontmost_bundle_id, restore_frontmost

    front = frontmost_bundle_id()
    return lambda: restore_frontmost(front)


def _age(ts: float) -> str:
    secs = max(0, int(time.time() - ts))
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if secs >= size:
            return f"{secs // size}{unit}"
    return f"{secs}s"


def _writes(args) -> int:
    from apple_notes import unified_log

    from .write_apply import WriteRefused
    from .write_queue import WriteQueue

    # apply/undo run remote-originated writes through the stdio tools: their
    # failures report like the server's (#170) - no titles, no error text.
    unified_log.configure(component="remote", redact=True)
    cfg = load_remote_config(args.config, require_secret=False)
    queue = WriteQueue(cfg.write_queue)
    cmd = args.writes_command
    if cmd == "list":
        rows = queue.list(limit=None if args.all else 20)
        state = "paused" if queue.paused() else "running"
        print(f"write queue {cfg.write_queue.state_dir} ({state}): {len(rows)} shown")
        for r in rows:
            op = r["tool"] if r["tool"] == r["operation"] else f"{r['tool']}/{r['operation']}"
            reason = f"  - {r['reason']}" if r["reason"] else ""
            print(f"{r['id']}  {r['status']:<18} {_age(r['created_at']):>4}  {op}  {r['note_title']!r}{reason}")
        return 0
    if cmd in ("pause", "resume"):
        queue.set_paused(cmd == "pause")
        print(f"Write queue {'paused' if cmd == 'pause' else 'resumed'}.")
        return 0
    if cmd == "clear":
        print(f"Rejected {queue.clear()} queued write(s).")
        return 0

    if cmd == "apply" and not (args.write_id or args.all):
        print("usage: writes apply <write_id> | writes apply --all", file=sys.stderr)
        return 2
    if not _session_available():
        print("The Mac's screen is locked: queued writes need an unlocked session.", file=sys.stderr)
        return 1
    from mcp.server.fastmcp.exceptions import ToolError

    from apple_notes import folder_scope

    # Same view of Notes as the server: never write into Recently Deleted.
    folder_scope.set_remote_mode(True)
    queue.expire()  # the pending limit holds even when the server isn't running
    applier = _make_applier(queue)
    with queue.drain_lock() as acquired:
        if not acquired:
            print("The server is already applying a write; try again in a moment.", file=sys.stderr)
            return 1
        restore = _restore_frontmost()
        try:
            if cmd == "undo":
                try:
                    print(f"{args.write_id}: {applier.undo(args.write_id)}")
                except WriteRefused as e:
                    print(f"{args.write_id}: not undone - {e}", file=sys.stderr)
                    return 1
                except ToolError as e:
                    print(f"{args.write_id}: undo failed - {e}", file=sys.stderr)
                    return 1
                return 0
            ids = [args.write_id] if args.write_id else [r["id"] for r in queue.pending()]
            for wid in ids:
                status = applier.apply(wid)
                print(f"{wid}: {status}")
                if status == "queued":
                    break  # locked or busy: keep the rest in order
        finally:
            restore()
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if demo_mode.is_demo():
        # The remote connector exposes write and Reminders tools; demo mode
        # runs the read-only stdio server only.
        print(demo_mode.refusal("The remote connector"), file=sys.stderr)
        return 2
    try:
        if args.command == "revoke-all":
            from .auth import TokenStore

            cfg = load_remote_config(args.config, require_secret=False)
            count = TokenStore(cfg.state_dir).revoke_all()
            print(
                f"Revoked {count} tokens/codes in {cfg.state_dir}. Claude must sign in again."
            )
            return 0
        if args.command == "allow-client":
            from .auth import TokenStore

            cfg = load_remote_config(args.config, require_secret=False)
            TokenStore(cfg.state_dir).allow_client(args.client_id)
            print(f"Allowed connector client {args.client_id}. Sign in again from claude.ai.")
            return 0
        if args.command == "check-config":
            return _check_config(args.config)
        if args.command == "writes":
            return _writes(args)
        return _serve(args.config)
    except RemoteConfigError as e:
        print(f"apple-notes-mcp-remote: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
