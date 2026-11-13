"""Assemble the remote ASGI app (issue #167).

A second FastMCP instance (the SDK's, ``mcp>=1.30``) carrying allowlisted
copies of the stdio server's tools, with the SDK's OAuth authorization-server
routes backed by ``GitHubOAuthProvider``. The stdio ``server.mcp`` object is
only read, never modified.
"""

import collections
import json
import time
from typing import Any, Callable, Optional
from urllib.parse import urlparse

import httpx
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import (
    AuthSettings,
    ClientRegistrationOptions,
    RevocationOptions,
)
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings

from apple_notes import folder_scope

from .auth import GitHubOAuthProvider, TokenStore
from .config import RemoteConfig
from .request_log import RequestLog, RequestLogMiddleware
from .tools import MAX_RESULT_CHARS, remote_tools
from .write_apply import WriteApplier
from .write_queue import WriteQueue
from .write_worker import WriteWorker


def _result_chars(result: Any) -> int:
    if isinstance(result, tuple):
        content, structured = result
    elif isinstance(result, dict):
        content, structured = [], result
    else:
        content, structured = result, None
    size = sum(len(getattr(block, "text", "") or "") for block in content)
    if structured is not None:
        size = max(size, len(json.dumps(structured, default=str)))
    return size


class RemoteFastMCP(FastMCP):
    """FastMCP that logs every tool call (name + decision only) and caps result size."""

    def __init__(self, *args, request_log: RequestLog, **kwargs):
        super().__init__(*args, **kwargs)
        self._request_log = request_log

    async def call_tool(self, name: str, arguments: dict[str, Any]):
        token = get_access_token()
        who = {"tool": name[:100], "subject": token.subject if token else None}
        if self._tool_manager.get_tool(name) is None:
            self._request_log.event(
                "tool", decision="deny", reason="not exposed remotely", **who
            )
            raise ToolError(f"Unknown tool: {name}")
        try:
            result = await super().call_tool(name, arguments)
        except Exception as e:
            self._request_log.event(
                "tool", decision="allow", outcome=f"error:{type(e).__name__}", **who
            )
            raise
        size = _result_chars(result)
        if size > MAX_RESULT_CHARS:
            self._request_log.event(
                "tool", decision="allow", outcome="too_large", chars=size, **who
            )
            raise ToolError(
                f"Result too large for Claude ({size} characters, limit {MAX_RESULT_CHARS}). "
                "Narrow the request: a folder filter, a search query, get_note_graph with start_note + depth, "
                "or reminders_list with list_name, due_by or completed_since."
            )
        self._request_log.event(
            "tool", decision="allow", outcome="ok", chars=size, **who
        )
        return result


REGISTER_MAX_BODY = 16 * 1024
REGISTER_RATE_LIMIT = 10  # registrations per window, server-wide (only Claude registers)
REGISTER_RATE_WINDOW = 60.0


async def _plain_error(send, status: int, message: str) -> None:
    body = json.dumps({"error": "invalid_request", "error_description": message}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
        }
    )
    await send({"type": "http.response.body", "body": body})


class RegistrationGuard:
    """Bounds open dynamic client registration: body size and a rate limit."""

    def __init__(self, app):
        self.app = app
        self._recent: collections.deque = collections.deque()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path") != "/register" or scope.get("method") != "POST":
            await self.app(scope, receive, send)
            return
        now = time.monotonic()
        while self._recent and now - self._recent[0] > REGISTER_RATE_WINDOW:
            self._recent.popleft()
        if len(self._recent) >= REGISTER_RATE_LIMIT:
            await _plain_error(send, 429, "too many client registrations; try again later")
            return
        self._recent.append(now)
        declared = dict(scope.get("headers") or []).get(b"content-length")
        if declared and declared.isdigit() and int(declared) > REGISTER_MAX_BODY:
            await _plain_error(send, 413, "registration request too large")
            return
        body, more = b"", True
        while more:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body += message.get("body", b"")
            more = message.get("more_body", False)
            if len(body) > REGISTER_MAX_BODY:
                await _plain_error(send, 413, "registration request too large")
                return
        replayed = False

        async def replay():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


def build_remote_app(
    config: RemoteConfig,
    *,
    source: Optional[FastMCP] = None,
    reminders_store=None,
    http_client_factory: Optional[Callable[[], httpx.AsyncClient]] = None,
):
    """The remote ASGI app. Also sets the process-wide folder scope (and the
    remote-mode trash guard) from config.

    With ``reminders.enabled`` the Reminders tools (issue #171) are merged in,
    reading ``reminders_store`` or a real EventKit store, with the lists and
    write tools from the config's ``reminders`` section; otherwise
    ``reminders_store`` is ignored."""
    if source is None:
        from apple_notes_mcp import server as stdio_server

        source = stdio_server.mcp

    extra_sources = []
    instructions = source.instructions
    if config.reminders.enabled:
        from apple_notes_mcp.reminders.server import (
            build_reminders_mcp,
            reminders_instructions,
        )

        if reminders_store is None:
            from apple_notes_mcp.reminders.store import EventKitStore

            reminders_store = EventKitStore()
        extra_sources.append(
            build_reminders_mcp(
                reminders_store,
                never_complete_lists=config.reminders.never_complete_lists,
                create_lists=config.reminders.create_lists,
            )
        )
        instructions = "\n\n".join(
            filter(
                None,
                (instructions, reminders_instructions(config.reminders.write_tools)),
            )
        )

    log = RequestLog(config.log_path)
    applier = worker = None
    if config.write_tools:
        # Remote writes are queued (#169). The worker is built here but only
        # started by `serve` (cli._serve), so tests never spawn the drain.
        queue = WriteQueue(config.write_queue)
        applier = WriteApplier(queue, log=log)
        worker = WriteWorker(queue, applier)
    provider = GitHubOAuthProvider(
        config, TokenStore(config.state_dir), log, http_client_factory
    )
    folder_scope.set_folder_allowlist(config.folder_allowlist)
    folder_scope.set_remote_mode(True)  # never serve Recently Deleted

    public_host = urlparse(config.public_url).hostname
    remote = RemoteFastMCP(
        source.name,
        instructions=instructions,
        request_log=log,
        tools=remote_tools(
            source,
            config.write_tools,
            extra_sources,
            config.reminders.write_tools,
            applier=applier,
            worker=worker,
        ),
        auth_server_provider=provider,
        auth=AuthSettings(
            issuer_url=config.public_url,
            resource_server_url=config.mcp_url,
            # We are the only resource server this AS issues for.
            validate_token_resource=False,
            client_registration_options=ClientRegistrationOptions(enabled=True),
            revocation_options=RevocationOptions(enabled=True),
        ),
        host=config.host,
        port=config.port,
        streamable_http_path="/mcp",
        # Stateless + JSON: every request stands alone (survives restarts, no SSE).
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[
                public_host,
                f"{public_host}:*",
                "127.0.0.1:*",
                "localhost:*",
            ],
            allowed_origins=[
                config.public_url,
                "https://claude.ai",
                "https://claude.com",
                "http://127.0.0.1:*",
                "http://localhost:*",
            ],
        ),
    )
    remote.custom_route("/oauth/not-allowed", methods=["GET"])(provider.not_allowed_page)
    remote.custom_route("/oauth/consent", methods=["GET"])(provider.consent_page)
    remote.custom_route("/oauth/consent", methods=["POST"])(provider.consent_submit)
    remote.custom_route("/oauth/github/callback", methods=["GET"])(provider.github_callback)
    app = RequestLogMiddleware(RegistrationGuard(remote.streamable_http_app()), log)
    app.write_worker = worker
    return app
