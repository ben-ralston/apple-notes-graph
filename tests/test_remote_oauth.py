"""Remote mode end to end: Streamable HTTP + OAuth proxy to a FAKE GitHub (#167).

Drives the real Starlette app (SDK auth routes, bearer middleware, the
/oauth/github/callback route, the tool allowlist and folder scope) through
Starlette's TestClient. Upstream GitHub is an httpx.MockTransport, so no
network and no real OAuth app are involved.
"""

import asyncio
import base64
import re
import hashlib
import json
import os
import secrets
import stat
import urllib.parse

import httpx
import pytest
from starlette.testclient import TestClient

from apple_notes import config as notes_config
from apple_notes import folder_scope
from apple_notes.database import NotesDatabase
from apple_notes_mcp.remote import app as remote_app
from apple_notes_mcp.remote import tools as remote_tools
from apple_notes_mcp.remote.app import build_remote_app
from apple_notes_mcp.remote.auth import TokenStore
from apple_notes_mcp.remote.config import RemoteConfig

PUBLIC = "https://connector.example.com"
CLAUDE_CB = "https://claude.ai/api/mcp/auth_callback"
MCP_HEADERS = {
    "accept": "application/json, text/event-stream",
    "content-type": "application/json",
}


class FakeGitHub:
    """Upstream IdP stand-in: each authorization code maps to one GitHub user."""

    def __init__(self):
        self.users = {
            "owner-code": {"login": "octo-owner", "id": 1001},
            "eve-code": {"login": "eve", "id": 666},
            # same login, different account id (renamed/re-registered login)
            "imposter-code": {"login": "octo-owner", "id": 999},
        }

    def handler(self, request: httpx.Request) -> httpx.Response:
        if (
            request.url.host == "github.test"
            and request.url.path == "/login/oauth/access_token"
        ):
            form = urllib.parse.parse_qs(request.content.decode())
            assert form["client_id"] == ["gh-client-id"]
            assert form["client_secret"] == ["gh-client-secret"]
            assert form["redirect_uri"] == [PUBLIC + "/oauth/github/callback"]
            code = form["code"][0]
            if code not in self.users:
                return httpx.Response(200, json={"error": "bad_verification_code"})
            return httpx.Response(
                200, json={"access_token": f"gho_{code}", "token_type": "bearer"}
            )
        if request.url.host == "api.github.test" and request.url.path == "/user":
            token = request.headers["authorization"].split()[1].removeprefix("gho_")
            return httpx.Response(200, json=self.users[token])
        return httpx.Response(404)


@pytest.fixture
def cfg(tmp_path):
    return RemoteConfig(
        public_url=PUBLIC,
        allowed_github_login="octo-owner",
        allowed_github_id=1001,
        github_client_id="gh-client-id",
        github_client_secret="gh-client-secret",
        state_dir=tmp_path / "state",
        log_path=tmp_path / "logs" / "requests.log",
        folder_allowlist=("Daily",),
        github_authorize_url="https://github.test/login/oauth/authorize",
        github_token_url="https://github.test/login/oauth/access_token",
        github_user_url="https://api.github.test/user",
    )


@pytest.fixture
def client(cfg, scoped_db, monkeypatch, tmp_path):
    monkeypatch.setattr(NotesDatabase, "DEFAULT_PATH", scoped_db)
    monkeypatch.setattr(notes_config, "CONFIG_PATH", tmp_path / "no-config.json")
    fake = FakeGitHub()
    app = build_remote_app(
        cfg,
        http_client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(fake.handler)
        ),
    )
    with TestClient(app, base_url=PUBLIC) as c:
        yield c
    folder_scope.reset()


def _pkce():
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).decode().rstrip("=")


def _register(client):
    r = client.post(
        "/register",
        json={
            "redirect_uris": [CLAUDE_CB],
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "client_name": "Claude",
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["client_id"]


def _consent_form(client, url):
    r = client.get(url)
    assert r.status_code == 200, r.text
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert "Claude" in r.text  # the registered client_name is shown
    return dict(re.findall(r'name="(\w+)" value="([^"]*)"', r.text))


def _authorize(client, client_id):
    _verifier, challenge = _pkce()
    r = client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": CLAUDE_CB,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "claude-state",
        },
        follow_redirects=False,
    )
    assert r.status_code == 302, r.text
    return r.headers["location"]


def _start_sign_in(client, client_id):
    verifier, challenge = _pkce()
    r = client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": CLAUDE_CB,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "claude-state",
        },
        follow_redirects=False,
    )
    assert r.status_code == 302, r.text
    consent = urllib.parse.urlparse(r.headers["location"])
    assert (consent.netloc, consent.path) == ("connector.example.com", "/oauth/consent")
    form = _consent_form(client, r.headers["location"])
    r = client.post("/oauth/consent", data=form, follow_redirects=False)
    assert r.status_code == 302, r.text
    loc = urllib.parse.urlparse(r.headers["location"])
    assert (loc.scheme, loc.netloc, loc.path) == (
        "https",
        "github.test",
        "/login/oauth/authorize",
    )
    query = urllib.parse.parse_qs(loc.query)
    assert query["client_id"] == ["gh-client-id"]
    assert query["redirect_uri"] == [PUBLIC + "/oauth/github/callback"]
    return query["state"][0], verifier


def _sign_in(client, upstream_code="owner-code"):
    client_id = _register(client)
    gh_state, verifier = _start_sign_in(client, client_id)
    r = client.get(
        "/oauth/github/callback",
        params={"code": upstream_code, "state": gh_state},
        follow_redirects=False,
    )
    return client_id, verifier, r


def _tokens(client, upstream_code="owner-code"):
    client_id, verifier, r = _sign_in(client, upstream_code)
    assert r.status_code == 302, r.text
    loc = urllib.parse.urlparse(r.headers["location"])
    assert f"{loc.scheme}://{loc.netloc}{loc.path}" == CLAUDE_CB
    query = urllib.parse.parse_qs(loc.query)
    assert query["state"] == ["claude-state"]
    r = client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": query["code"][0],
            "redirect_uri": CLAUDE_CB,
            "client_id": client_id,
            "code_verifier": verifier,
        },
    )
    assert r.status_code == 200, r.text
    return client_id, r.json()


def _rpc(client, token, method, params=None, headers=None):
    h = dict(MCP_HEADERS)
    if token:
        h["authorization"] = f"Bearer {token}"
    h.update(headers or {})
    return client.post(
        "/mcp",
        headers=h,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
    )


def _call(client, token, name, arguments=None):
    r = _rpc(client, token, "tools/call", {"name": name, "arguments": arguments or {}})
    assert r.status_code == 200, r.text
    return r.json()["result"]


def _log(cfg):
    return [json.loads(line) for line in cfg.log_path.read_text().splitlines()]


def test_metadata_endpoints(client):
    r = client.get("/.well-known/oauth-protected-resource/mcp")
    assert r.status_code == 200
    assert r.json()["authorization_servers"] == [PUBLIC + "/"]
    r = client.get("/.well-known/oauth-authorization-server")
    meta = r.json()
    assert meta["code_challenge_methods_supported"] == ["S256"]
    assert meta["registration_endpoint"] == PUBLIC + "/register"
    assert "refresh_token" in meta["grant_types_supported"]


def test_unauthenticated_mcp_is_401_pointing_at_resource_metadata(client):
    r = _rpc(client, None, "tools/list")
    assert r.status_code == 401
    assert (
        f'resource_metadata="{PUBLIC}/.well-known/oauth-protected-resource/mcp"'
        in r.headers["www-authenticate"]
    )


def test_registration_rejects_foreign_redirect_uri(client):
    r = client.post(
        "/register",
        json={
            "redirect_uris": ["https://evil.example/cb"],
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
        },
    )
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_redirect_uri"


def test_allowed_identity_gets_short_lived_tokens_and_read_only_tools(client):
    _cid, tokens = _tokens(client)
    assert 0 < tokens["expires_in"] <= 3600
    assert tokens["refresh_token"]
    r = _rpc(client, tokens["access_token"], "tools/list")
    assert r.status_code == 200, r.text
    tools = {t["name"]: t for t in r.json()["result"]["tools"]}
    assert set(tools) == remote_tools.READ_TOOLS
    assert tools["read_note_content"]["annotations"]["readOnlyHint"] is True


@pytest.mark.parametrize("upstream_code", ["eve-code", "imposter-code"])
def test_other_github_identities_are_refused(client, cfg, upstream_code):
    _cid, _verifier, r = _sign_in(client, upstream_code)
    assert r.status_code == 403
    assert "location" not in r.headers
    denials = [e for e in _log(cfg) if e.get("event") == "sign_in"]
    assert denials[-1]["decision"] == "deny"


def test_unknown_upstream_state_is_rejected(client):
    r = client.get(
        "/oauth/github/callback",
        params={"code": "owner-code", "state": "forged"},
        follow_redirects=False,
    )
    assert r.status_code == 400


def test_upstream_state_is_single_use(client):
    client_id = _register(client)
    gh_state, _ = _start_sign_in(client, client_id)
    params = {"code": "owner-code", "state": gh_state}
    assert (
        client.get(
            "/oauth/github/callback", params=params, follow_redirects=False
        ).status_code
        == 302
    )
    assert (
        client.get(
            "/oauth/github/callback", params=params, follow_redirects=False
        ).status_code
        == 400
    )


def test_refresh_token_rotates(client):
    client_id, tokens = _tokens(client)
    form = {
        "grant_type": "refresh_token",
        "refresh_token": tokens["refresh_token"],
        "client_id": client_id,
    }
    r = client.post("/token", data=form)
    assert r.status_code == 200, r.text
    new = r.json()
    assert new["refresh_token"] != tokens["refresh_token"]
    assert _rpc(client, new["access_token"], "tools/list").status_code == 200
    # the used refresh token is dead, and so is the access token it replaced
    assert client.post("/token", data=form).status_code == 400
    assert _rpc(client, tokens["access_token"], "tools/list").status_code == 401


def test_revoke_all_kills_live_tokens(client, cfg):
    client_id, tokens = _tokens(client)
    assert _rpc(client, tokens["access_token"], "tools/list").status_code == 200
    TokenStore(
        cfg.state_dir
    ).revoke_all()  # what `apple-notes-mcp-remote revoke-all` does
    assert _rpc(client, tokens["access_token"], "tools/list").status_code == 401
    r = client.post(
        "/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
            "client_id": client_id,
        },
    )
    assert r.status_code in (400, 401)


def test_never_remote_tool_call_is_denied_and_logged(client, cfg):
    _cid, tokens = _tokens(client)
    result = _call(
        client,
        tokens["access_token"],
        "export_note",
        {"name": "Today", "output_dir": "/tmp/x"},
    )
    assert result["isError"] is True
    entry = [e for e in _log(cfg) if e.get("event") == "tool"][-1]
    assert entry["tool"] == "export_note" and entry["decision"] == "deny"
    assert entry["subject"] == "octo-owner"


def test_read_tools_only_see_allowlisted_folders(client):
    _cid, tokens = _tokens(client)
    folders = _call(
        client, tokens["access_token"], "list_folders", {"include_all": True}
    )
    text = json.dumps(folders)
    assert "Daily" in text and "Private" not in text and "Daily Archive" not in text
    notes = _call(
        client,
        tokens["access_token"],
        "search_notes",
        {"query": "Secret", "include_all": True},
    )
    assert "Secret" not in json.dumps(notes)


def test_log_has_tool_and_decision_but_no_tokens_codes_or_arguments(client, cfg):
    client_id, tokens = _tokens(client)
    _call(client, tokens["access_token"], "search_notes", {"query": "Last year"})
    raw = cfg.log_path.read_text()
    entry = [e for e in _log(cfg) if e.get("event") == "tool"][-1]
    assert entry["tool"] == "search_notes" and entry["decision"] == "allow"
    assert "time" in entry
    for secret in (
        tokens["access_token"],
        tokens["refresh_token"],
        "owner-code",
        "gho_owner-code",
        "Last year",
    ):
        assert secret not in raw
    assert any(e.get("event") == "http" and e["path"] == "/mcp" for e in _log(cfg))


def test_token_store_is_private_and_holds_no_raw_tokens(client, cfg):
    _cid, tokens = _tokens(client)
    store_file = cfg.state_dir / "tokens.json"
    assert stat.S_IMODE(os.stat(store_file).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(cfg.state_dir).st_mode) == 0o700
    raw = store_file.read_text()
    assert tokens["access_token"] not in raw and tokens["refresh_token"] not in raw


def test_unknown_host_header_is_rejected(client):
    _cid, tokens = _tokens(client)
    r = _rpc(
        client, tokens["access_token"], "tools/list", headers={"host": "evil.example"}
    )
    assert r.status_code in (400, 403, 421)


def test_mcp_endpoint_does_not_redirect(client):
    _cid, tokens = _tokens(client)
    r = _rpc(client, tokens["access_token"], "tools/list")
    assert r.status_code == 200 and not r.history


def test_consent_post_without_the_browser_cookie_is_refused(client):
    # A cross-site auto-submitted form carries no SameSite=Strict cookie.
    form = _consent_form(client, _authorize(client, _register(client)))
    client.cookies.clear()
    r = client.post("/oauth/consent", data=form, follow_redirects=False)
    assert r.status_code == 403
    assert "location" not in r.headers


def test_consent_post_with_wrong_csrf_is_refused(client):
    form = _consent_form(client, _authorize(client, _register(client)))
    r = client.post("/oauth/consent", data={**form, "csrf": "forged"}, follow_redirects=False)
    assert r.status_code == 403


def test_registered_clients_are_capped_and_names_truncated_in_log(client, cfg, monkeypatch):
    monkeypatch.setattr(remote_app, "REGISTER_RATE_LIMIT", 1000)
    from apple_notes_mcp.remote.auth import MAX_CLIENTS

    for i in range(MAX_CLIENTS + 5):
        r = client.post(
            "/register",
            json={
                "redirect_uris": [CLAUDE_CB],
                "token_endpoint_auth_method": "none",
                "grant_types": ["authorization_code", "refresh_token"],
                "client_name": f"c{i}-" + "x" * 500,
            },
        )
        assert r.status_code == 201
    store = json.loads((cfg.state_dir / "tokens.json").read_text())
    assert len(store["clients"]) == MAX_CLIENTS
    names = [e["client_name"] for e in _log(cfg) if e.get("event") == "register"]
    assert max(len(n) for n in names) <= 80
    assert names[-1].startswith(f"c{MAX_CLIENTS + 4}-")


def test_slow_upstream_fails_within_one_overall_deadline(cfg, scoped_db, monkeypatch, tmp_path):
    from apple_notes_mcp.remote import auth

    monkeypatch.setattr(auth, "UPSTREAM_DEADLINE", 0.3)

    async def slow(request):
        await asyncio.sleep(0.2)  # each call alone is under the deadline
        return FakeGitHub().handler(request)

    app = build_remote_app(
        cfg, http_client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(slow))
    )
    with TestClient(app, base_url=PUBLIC) as c:
        gh_state, _ = _start_sign_in(c, _register(c))
        r = c.get(
            "/oauth/github/callback",
            params={"code": "owner-code", "state": gh_state},
            follow_redirects=False,
        )
    folder_scope.reset()
    assert r.status_code == 502


def test_consent_approved_in_another_browser_cannot_be_completed_in_bens(client):
    # Attacker approves in their own browser, then sends the owner the GitHub URL:
    # the owner's browser lacks the flow cookie set on approval, so no code is issued.
    gh_state, _ = _start_sign_in(client, _register(client))
    client.cookies.clear()
    r = client.get(
        "/oauth/github/callback",
        params={"code": "owner-code", "state": gh_state},
        follow_redirects=False,
    )
    assert r.status_code == 400
    assert "location" not in r.headers


def test_signed_in_client_survives_a_registration_flood(client, monkeypatch):
    monkeypatch.setattr(remote_app, "REGISTER_RATE_LIMIT", 1000)
    from apple_notes_mcp.remote.auth import MAX_CLIENTS

    client_id, tokens = _tokens(client)
    flood = {
        "redirect_uris": [CLAUDE_CB],
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"],
    }
    statuses = [client.post("/register", json=flood).status_code for _ in range(MAX_CLIENTS + 5)]
    assert set(statuses) == {201}  # unused flood clients evict each other
    r = client.post(
        "/token",
        data={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"], "client_id": client_id},
    )
    assert r.status_code == 200, r.text



def _state_of(url):
    return urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["state"][0]


def _register_status(client, **extra):
    body = {
        "redirect_uris": [CLAUDE_CB],
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"],
        **extra,
    }
    return client.post("/register", json=body)


# M1: trust on first use - the first client to finish sign-in is pinned
def test_first_signed_in_client_is_pinned_and_others_refused(client):
    first_id, _tokens_ = _tokens(client)
    other = _register(client)
    url = _authorize(client, other)
    r = client.get(url)
    assert r.status_code == 403
    assert other in r.text and "remote-allow-client" in r.text
    assert 'name="csrf"' not in r.text
    r = client.post(
        "/oauth/consent",
        data={"state": "not-a-parked-sign-in", "csrf": "x", "decision": "approve"},
        follow_redirects=False,
    )
    assert r.status_code == 403
    _start_sign_in(client, first_id)  # the pinned client still gets the consent page


def test_allow_client_admits_a_new_client(client, cfg):
    _tokens(client)
    other = _register(client)
    TokenStore(cfg.state_dir).allow_client(other)
    _start_sign_in(client, other)


def test_revoke_all_clears_the_pin(client, cfg):
    _tokens(client)
    other = _register(client)
    TokenStore(cfg.state_dir).revoke_all()
    _start_sign_in(client, other)


def test_consent_page_shows_the_client_id(client):
    cid = _register(client)
    assert cid in client.get(_authorize(client, cid)).text


# L6: the consent CSRF value works once
def test_consent_csrf_is_single_use(client):
    form = _consent_form(client, _authorize(client, _register(client)))
    assert client.post("/oauth/consent", data=form, follow_redirects=False).status_code == 302
    assert client.post("/oauth/consent", data=form, follow_redirects=False).status_code == 403


# L3: revoke-all also kills sign-ins already in flight in the running server
def test_revoke_all_cancels_in_flight_sign_ins(client, cfg):
    gh_state, _ = _start_sign_in(client, _register(client))
    TokenStore(cfg.state_dir).revoke_all()
    r = client.get(
        "/oauth/github/callback",
        params={"code": "owner-code", "state": gh_state},
        follow_redirects=False,
    )
    assert r.status_code == 400


# L2: token-family reuse detection
def test_refresh_token_reuse_revokes_the_family(client):
    client_id, tokens = _tokens(client)
    form = {"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"], "client_id": client_id}
    new = client.post("/token", data=form).json()
    assert client.post("/token", data=form).status_code == 400  # replayed old refresh token
    assert _rpc(client, new["access_token"], "tools/list").status_code == 401
    r = client.post("/token", data={**form, "refresh_token": new["refresh_token"]})
    assert r.status_code == 400


def test_code_reuse_revokes_tokens_issued_from_it(client):
    client_id, verifier, r = _sign_in(client)
    code = urllib.parse.parse_qs(urllib.parse.urlparse(r.headers["location"]).query)["code"][0]
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": CLAUDE_CB,
        "client_id": client_id,
        "code_verifier": verifier,
    }
    first = client.post("/token", data=form).json()
    assert client.post("/token", data=form).status_code == 400
    assert _rpc(client, first["access_token"], "tools/list").status_code == 401


# M3: registration limits
def test_register_body_is_capped(client):
    big = json.dumps({"redirect_uris": [CLAUDE_CB], "client_name": "x" * 20000})
    r = client.post("/register", content=big, headers={"content-type": "application/json"})
    assert r.status_code == 413


def test_registration_is_rate_limited(client):
    statuses = [_register_status(client).status_code for _ in range(remote_app.REGISTER_RATE_LIMIT + 1)]
    assert statuses[:-1] == [201] * remote_app.REGISTER_RATE_LIMIT
    assert statuses[-1] == 429


def test_registration_stores_only_used_fields_with_caps(client, cfg):
    r = _register_status(
        client,
        client_name="C" * 500,
        logo_uri="https://claude.ai/" + "x" * 200,
        contacts=["a@example.com"] * 50,
        tos_uri="https://claude.ai/tos",
        software_statement="y" * 500,
    )
    assert r.status_code == 201
    stored = json.loads((cfg.state_dir / "tokens.json").read_text())["clients"][r.json()["client_id"]]
    for dropped in ("logo_uri", "contacts", "tos_uri", "software_statement", "jwks"):
        assert dropped not in stored
    assert len(stored["client_name"]) <= 80


# L1: pending sign-ins are capped (oldest dropped)
def test_pending_sign_ins_are_capped(client):
    from apple_notes_mcp.remote.auth import MAX_PENDING

    cid = _register(client)
    oldest = _authorize(client, cid)
    for _ in range(MAX_PENDING):
        _authorize(client, cid)
    assert client.get(oldest).status_code == 400


# L5: a client mid-sign-in is not evicted by a registration flood
def test_client_mid_sign_in_survives_a_registration_flood(client, cfg, monkeypatch):
    from apple_notes_mcp.remote.auth import MAX_CLIENTS

    monkeypatch.setattr(remote_app, "REGISTER_RATE_LIMIT", 1000)
    cid = _register(client)
    url = _authorize(client, cid)
    for _ in range(MAX_CLIENTS + 5):
        assert _register_status(client).status_code == 201
    assert cid in json.loads((cfg.state_dir / "tokens.json").read_text())["clients"]
    form = _consent_form(client, url)
    assert client.post("/oauth/consent", data=form, follow_redirects=False).status_code == 302


# --- re-review round: pin race, pending pressure, revoke mid-callback ------
def _app_with(cfg, scoped_db, monkeypatch, tmp_path, handler_factory):
    monkeypatch.setattr(NotesDatabase, "DEFAULT_PATH", scoped_db)
    monkeypatch.setattr(notes_config, "CONFIG_PATH", tmp_path / "no-config.json")
    fake = FakeGitHub()
    handler = handler_factory(fake)
    return build_remote_app(
        cfg, http_client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )


def test_concurrent_callbacks_only_the_pinning_client_gets_a_code(cfg, scoped_db, monkeypatch, tmp_path):
    import threading
    import time as _time

    def slow_user(fake):
        def handler(request):
            if request.url.path == "/user":
                _time.sleep(0.5)  # both callbacks are inside the GitHub round trip together
            return fake.handler(request)

        return handler

    app = _app_with(cfg, scoped_db, monkeypatch, tmp_path, slow_user)
    with TestClient(app, base_url=PUBLIC) as c1:
        c2 = TestClient(app, base_url=PUBLIC)  # second browser; lifespan already running
        a, b = _register(c1), _register(c2)
        sa, _ = _start_sign_in(c1, a)
        sb, _ = _start_sign_in(c2, b)
        out = {}

        def go(name, c, state):
            out[name] = c.get(
                "/oauth/github/callback", params={"code": "owner-code", "state": state}, follow_redirects=False
            )

        threads = [threading.Thread(target=go, args=("a", c1, sa)), threading.Thread(target=go, args=("b", c2, sb))]
        [t.start() for t in threads]
        [t.join() for t in threads]
    folder_scope.reset()
    statuses = sorted(r.status_code for r in out.values())
    assert statuses == [302, 403], statuses


def test_unpinned_clients_are_refused_at_the_token_endpoint(client, cfg):
    client_id, tokens = _tokens(client)
    store_file = cfg.state_dir / "tokens.json"
    data = json.loads(store_file.read_text())
    data["meta"]["allowed_clients"] = ["some-other-client"]  # pin moved away from this client
    store_file.write_text(json.dumps(data))
    assert _rpc(client, tokens["access_token"], "tools/list").status_code == 401
    r = client.post(
        "/token",
        data={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"], "client_id": client_id},
    )
    assert r.status_code in (400, 401)


def test_unpinned_client_cannot_park_sign_ins_or_evict_bens(client):
    from apple_notes_mcp.remote.auth import MAX_PENDING

    owner, _ = _tokens(client)
    owner_state, _ = _start_sign_in(client, owner)  # the owner re-signs in; in flight
    attacker = TestClient(client.app, base_url=PUBLIC)  # separate browser
    evil = _register(attacker)
    for _ in range(MAX_PENDING):
        loc = _authorize(attacker, evil)
    assert urllib.parse.urlparse(loc).path == "/oauth/not-allowed"
    r = attacker.get(loc)
    assert r.status_code == 403 and evil in r.text
    r = client.get(
        "/oauth/github/callback", params={"code": "owner-code", "state": owner_state}, follow_redirects=False
    )
    assert r.status_code == 302


def test_one_client_cannot_crowd_out_other_pending_sign_ins(client):
    from apple_notes_mcp.remote.auth import MAX_PENDING

    noisy, quiet = _register(client), _register(client)
    quiet_url = _authorize(client, quiet)
    for _ in range(MAX_PENDING + 5):
        _authorize(client, noisy)
    assert client.get(quiet_url).status_code == 200


def test_attacker_clients_cannot_fill_the_client_table(client, monkeypatch):
    from apple_notes_mcp.remote.auth import MAX_CLIENTS

    monkeypatch.setattr(remote_app, "REGISTER_RATE_LIMIT", 10_000)
    _tokens(client)  # the owner pinned, live grants
    for _ in range(MAX_CLIENTS - 1):
        _authorize(client, _register(client))
    assert _register_status(client).status_code == 201


def test_revoke_during_the_github_round_trip_issues_no_code_and_no_pin(cfg, scoped_db, monkeypatch, tmp_path):
    def revoke_mid_flight(fake):
        def handler(request):
            if request.url.path == "/user":
                TokenStore(cfg.state_dir).revoke_all()  # the CLI, mid-callback
            return fake.handler(request)

        return handler

    app = _app_with(cfg, scoped_db, monkeypatch, tmp_path, revoke_mid_flight)
    with TestClient(app, base_url=PUBLIC) as c:
        _cid, _verifier, r = _sign_in(c)
    folder_scope.reset()
    assert r.status_code == 400
    assert TokenStore(cfg.state_dir)._read()["meta"]["allowed_clients"] == []


@pytest.mark.parametrize("before", ["pin_if_first", "put_code"])
def test_revoke_after_the_callback_epoch_check_issues_no_code_and_no_pin(cfg, scoped_db, monkeypatch, tmp_path, before):
    """revoke-all landing after the callback's epoch check but before the pin or
    the code is written (#192): neither may land in the just-wiped store."""
    original = getattr(TokenStore, before)

    def revoke_then(self, *args, **kwargs):
        TokenStore(cfg.state_dir).revoke_all()  # the CLI, mid-callback
        return original(self, *args, **kwargs)

    monkeypatch.setattr(TokenStore, before, revoke_then)
    app = _app_with(cfg, scoped_db, monkeypatch, tmp_path, lambda fake: fake.handler)
    with TestClient(app, base_url=PUBLIC) as c:
        _cid, _verifier, r = _sign_in(c)
    folder_scope.reset()
    assert r.status_code == 400
    data = TokenStore(cfg.state_dir)._read()
    assert data["meta"]["allowed_clients"] == [] and data["codes"] == {}


def test_losing_a_concurrent_exchange_revokes_the_family(cfg, tmp_path):
    from mcp.shared.auth import OAuthClientInformationFull
    from mcp.server.auth.provider import TokenError

    from apple_notes_mcp.remote.auth import GitHubOAuthProvider
    from apple_notes_mcp.remote.request_log import RequestLog

    store = TokenStore(cfg.state_dir)
    provider = GitHubOAuthProvider(cfg, store, RequestLog(None))
    client_info = OAuthClientInformationFull(client_id="c1", redirect_uris=[CLAUDE_CB], token_endpoint_auth_method="none")
    store.put_code(
        "the-code",
        {"code": "h", "scopes": [], "expires_at": 9e12, "client_id": "c1", "code_challenge": "x",
         "redirect_uri": CLAUDE_CB, "redirect_uri_provided_explicitly": True, "subject": "octo-owner"},
        epoch=store.epoch(),
    )

    async def race():
        loaded = await provider.load_authorization_code(client_info, "the-code")  # both loaded first
        first = await provider.exchange_authorization_code(client_info, loaded)
        with pytest.raises(TokenError):
            await provider.exchange_authorization_code(client_info, loaded)
        assert store.get_access(first.access_token) is None
        r1 = await provider.load_refresh_token(client_info, first.refresh_token)
        assert r1 is None  # family revoked
        # same for a refresh token exchanged twice concurrently
        store.put_code("code-2", {"code": "h", "scopes": [], "expires_at": 9e12, "client_id": "c1", "code_challenge": "x",
                                  "redirect_uri": CLAUDE_CB, "redirect_uri_provided_explicitly": True, "subject": "octo-owner"},
                       epoch=store.epoch())
        tokens = await provider.exchange_authorization_code(
            client_info, await provider.load_authorization_code(client_info, "code-2")
        )
        rt = await provider.load_refresh_token(client_info, tokens.refresh_token)
        new = await provider.exchange_refresh_token(client_info, rt, [])
        with pytest.raises(TokenError):
            await provider.exchange_refresh_token(client_info, rt, [])
        assert store.get_access(new.access_token) is None

    asyncio.run(race())


@pytest.mark.parametrize("grant", ["authorization_code", "refresh_token"])
def test_revoke_between_pop_and_put_issues_no_tokens(cfg, monkeypatch, grant):
    """revoke-all landing mid-exchange (#192): the old grant is already gone, and
    the new tokens must not be written into the just-wiped store."""
    from mcp.shared.auth import OAuthClientInformationFull
    from mcp.server.auth.provider import TokenError

    from apple_notes_mcp.remote.auth import GitHubOAuthProvider
    from apple_notes_mcp.remote.request_log import RequestLog

    store = TokenStore(cfg.state_dir)
    provider = GitHubOAuthProvider(cfg, store, RequestLog(None))
    client_info = OAuthClientInformationFull(client_id="c1", redirect_uris=[CLAUDE_CB], token_endpoint_auth_method="none")
    store.put_code(
        "the-code",
        {"code": "h", "scopes": [], "expires_at": 9e12, "client_id": "c1", "code_challenge": "x",
         "redirect_uri": CLAUDE_CB, "redirect_uri_provided_explicitly": True, "subject": "octo-owner"},
        epoch=store.epoch(),
    )

    def revoke_after(name):
        original = getattr(store, name)

        def pop_then_revoke(*args, **kwargs):
            entry = original(*args, **kwargs)
            TokenStore(cfg.state_dir).revoke_all()  # the CLI, mid-exchange
            return entry

        monkeypatch.setattr(store, name, pop_then_revoke)

    async def race():
        code = await provider.load_authorization_code(client_info, "the-code")
        if grant == "authorization_code":
            revoke_after("consume_code")
            return await provider.exchange_authorization_code(client_info, code)
        tokens = await provider.exchange_authorization_code(client_info, code)
        rt = await provider.load_refresh_token(client_info, tokens.refresh_token)
        revoke_after("pop_refresh")
        return await provider.exchange_refresh_token(client_info, rt, [])

    with pytest.raises(TokenError) as e:
        asyncio.run(race())
    assert e.value.error == "invalid_grant"
    data = TokenStore(cfg.state_dir)._read()
    assert data["access"] == {} and data["refresh"] == {} and data["codes"] == {}


def test_put_tokens_refuses_a_stale_epoch(tmp_path):
    from apple_notes_mcp.remote.auth import RevokedError

    store = TokenStore(tmp_path)
    epoch = store.epoch()
    store.put_tokens("a1", {"client_id": "c1", "expires_at": 9e12}, "r1", {"client_id": "c1", "expires_at": 9e12}, epoch=epoch)
    assert store.get_access("a1") is not None  # same epoch: written
    store.revoke_all()
    with pytest.raises(RevokedError):
        store.put_tokens("a2", {"client_id": "c1", "expires_at": 9e12}, "r2", {"client_id": "c1", "expires_at": 9e12}, epoch=epoch)
    assert store.get_access("a2") is None and store.get_refresh("r2") is None


def test_not_allowed_page_only_names_registered_well_formed_client_ids(client):
    r = client.get("/oauth/not-allowed", params={"client_id": "<script>alert(1)</script>"})
    assert r.status_code == 403 and "<script>" not in r.text and "remote-allow-client" not in r.text
    r = client.get("/oauth/not-allowed", params={"client_id": "made-up-id-never-registered"})
    assert "made-up-id-never-registered" not in r.text and "remote-allow-client" not in r.text
    real = _register(client)
    assert real in client.get("/oauth/not-allowed", params={"client_id": real}).text


def test_not_allowed_page_offers_no_command_for_an_unknown_client(client):
    r = client.get("/oauth/not-allowed", params={"client_id": "made-up-id"})
    assert "remote-allow-client" not in r.text


def _reminders_client(cfg, scoped_db, monkeypatch, tmp_path, store, reminders=None):
    import dataclasses

    from apple_notes_mcp.remote.config import RemindersConfig

    monkeypatch.setattr(NotesDatabase, "DEFAULT_PATH", scoped_db)
    monkeypatch.setattr(notes_config, "CONFIG_PATH", tmp_path / "no-config.json")
    fake = FakeGitHub()
    app = build_remote_app(
        dataclasses.replace(
            cfg,
            reminders=reminders or RemindersConfig(enabled=True),
            # Its own token store: the TOFU pin is per store.
            state_dir=tmp_path / "reminders-state",
        ),
        reminders_store=store,
        http_client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(fake.handler)
        ),
    )
    return TestClient(app, base_url=PUBLIC)


def _tool_names(client, token):
    r = _rpc(client, token, "tools/list")
    assert r.status_code == 200, r.text
    return {t["name"] for t in r.json()["result"]["tools"]}


def test_reminders_tools_listed_only_when_enabled(client, cfg, scoped_db, monkeypatch, tmp_path):
    from reminders_fakes import FakeStore

    _cid, tokens = _tokens(client)
    assert "reminders_list" not in _tool_names(client, tokens["access_token"])
    folder_scope.reset()
    with _reminders_client(cfg, scoped_db, monkeypatch, tmp_path / "r", FakeStore()) as rc:
        _cid, tokens = _tokens(rc)
        names = _tool_names(rc, tokens["access_token"])
    folder_scope.reset()
    assert {"reminders_lists", "reminders_list", "list_folders"} <= names
    assert not names & {"reminders_complete", "reminders_create"}


def test_reminders_call_is_logged_without_titles(cfg, scoped_db, monkeypatch, tmp_path):
    from reminders_fakes import FakeStore

    with _reminders_client(cfg, scoped_db, monkeypatch, tmp_path, FakeStore()) as rc:
        _cid, tokens = _tokens(rc)
        result = _call(rc, tokens["access_token"], "reminders_list")
    folder_scope.reset()
    assert result.get("isError") is not True
    assert "Pay rent" in json.dumps(result)
    entry = [e for e in _log(cfg) if e.get("event") == "tool"][-1]
    assert entry["tool"] == "reminders_list" and entry["decision"] == "allow"
    raw = cfg.log_path.read_text()
    for title in ("Pay rent", "Call mom", "Milk"):
        assert title not in raw


def test_app_starts_and_lists_tools_when_reminders_access_is_missing(
    cfg, scoped_db, monkeypatch, tmp_path
):
    from reminders_fakes import FakeStore

    store = FakeStore(status="not-determined")
    with _reminders_client(cfg, scoped_db, monkeypatch, tmp_path, store) as rc:
        _cid, tokens = _tokens(rc)
        names = _tool_names(rc, tokens["access_token"])
        result = _call(rc, tokens["access_token"], "reminders_lists")
    folder_scope.reset()
    assert {"list_folders", "reminders_lists"} <= names
    assert result["isError"] is True
    assert "--probe-reminders" in json.dumps(result)


def test_oversized_reminders_result_suggests_narrowing(cfg, scoped_db, monkeypatch, tmp_path):
    from reminders_fakes import FakeStore, _rem

    many = [_rem(f"r{i}", "t" * 1000, "a") for i in range(200)]
    with _reminders_client(cfg, scoped_db, monkeypatch, tmp_path, FakeStore(reminders=many)) as rc:
        _cid, tokens = _tokens(rc)
        result = _call(rc, tokens["access_token"], "reminders_list")
    folder_scope.reset()
    assert result["isError"] is True
    assert "reminders_list with list_name, due_by or completed_since" in json.dumps(result)


def test_reminders_write_tools_listed_when_opted_in(cfg, scoped_db, monkeypatch, tmp_path):
    from apple_notes_mcp.remote.config import RemindersConfig
    from reminders_fakes import FakeStore

    reminders = RemindersConfig(
        enabled=True,
        never_complete_lists=("Grocery List",),
        create_lists=("Grocery List",),
        write_tools=("reminders_complete", "reminders_create"),
    )
    store = FakeStore()
    with _reminders_client(cfg, scoped_db, monkeypatch, tmp_path, store, reminders) as rc:
        _cid, tokens = _tokens(rc)
        token = tokens["access_token"]
        listed = {t["name"]: t for t in _rpc(rc, token, "tools/list").json()["result"]["tools"]}
        blocked = _call(rc, token, "reminders_complete", {"id": "r4", "expected_due": "2026-10-30"})
        done = _call(rc, token, "reminders_complete", {"id": "r1", "expected_due": "2026-10-24"})
        outside = _call(rc, token, "reminders_create", {"title": "Eggs", "list": "b"})
    folder_scope.reset()
    assert listed["reminders_complete"]["annotations"]["destructiveHint"] is True
    assert listed["reminders_create"]["annotations"]["destructiveHint"] is False
    # remote.json's lists reach the tools.
    assert blocked["isError"] is True and "reminders.never_complete_lists" in json.dumps(blocked)
    assert outside["isError"] is True and "reminders.create_lists" in json.dumps(outside)
    assert done.get("isError") is not True and store.completed == ["r1"]
    entries = [e for e in _log(cfg) if e.get("event") == "tool"]
    assert {e["tool"] for e in entries} == {"reminders_complete", "reminders_create"}
    raw = cfg.log_path.read_text()
    for text in ("Pay rent", "Milk", "Eggs", "2026-10-24"):
        assert text not in raw
