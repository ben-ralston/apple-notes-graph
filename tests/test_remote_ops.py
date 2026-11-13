"""Remote-mode operations: CLI, kill switch, LaunchAgent plist, stdio guard (#167).

Nothing here touches launchctl, ~/Library/LaunchAgents, or a tunnel: the kill
switch and ui-helper-install run against PATH stubs, and the plists are only
rendered and parsed.
"""

import json
import os
import plistlib
import shutil
import subprocess
import tomllib
from pathlib import Path

from apple_notes_mcp import server
from apple_notes_mcp.remote import cli
from apple_notes_mcp.remote.auth import TokenStore

ROOT = Path(__file__).resolve().parent.parent


def test_stdio_main_still_just_runs_the_stdio_server(monkeypatch):
    calls = []
    monkeypatch.setattr(server.mcp, "run", lambda *a, **k: calls.append((a, k)))
    server.main()
    assert calls == [((), {})]
    assert server.mcp.settings.auth is None


def test_entry_points():
    scripts = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["scripts"]
    assert scripts["apple-notes-mcp"] == "apple_notes_mcp.server:main"
    assert scripts["apple-notes-mcp-remote"] == "apple_notes_mcp.remote.cli:main"


def test_revoke_all_command_wipes_the_store(tmp_path, capsys):
    store = TokenStore(tmp_path / "state")
    store.put_access(
        "tok-a", {"client_id": "c", "subject": "owner", "expires_at": 9e9, "scopes": []}
    )
    assert store.get_access("tok-a")
    config = tmp_path / "remote.json"
    config.write_text(
        '{"public_url": "https://n.example.com", "allowed_github_login": "owner",'
        f' "allowed_github_id": 1, "github_client_id": "x", "state_dir": "{tmp_path / "state"}"}}'
    )
    assert cli.main(["--config", str(config), "revoke-all"]) == 0
    assert store.get_access("tok-a") is None
    assert "revoked" in capsys.readouterr().out.lower()


def _stub_bin(tmp_path, name, exit_code=1):
    stub = tmp_path / "bin" / name
    stub.parent.mkdir(exist_ok=True)
    stub.write_text(f'#!/bin/sh\necho "{name} $*" >> "$STUB_LOG"\nexit {exit_code}\n')
    stub.chmod(0o755)


def test_kill_switch_stops_only_our_server_and_tunnel(tmp_path):
    for name in ("launchctl", "pkill", "tailscale"):
        _stub_bin(tmp_path, name)
    log = tmp_path / "calls.log"
    env = {
        "PATH": f"{tmp_path / 'bin'}:/usr/bin:/bin",
        "STUB_LOG": str(log),
        "HOME": str(tmp_path),
    }
    r = subprocess.run(
        [str(ROOT / "scripts" / "remote-kill-switch.sh")],
        env=env,
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr
    calls = log.read_text().splitlines()
    uid = os.getuid()
    assert f"launchctl bootout gui/{uid}/com.notes-graph.mcp-remote-tunnel" in calls
    assert f"launchctl bootout gui/{uid}/com.notes-graph.mcp-remote" in calls
    assert any(
        c.startswith("pkill -f") and "apple-notes-mcp-remote" in c for c in calls
    )
    # never a blanket kill of every cloudflared (other tunnels may use one)
    assert not any("cloudflared" in c for c in calls if c.startswith("pkill"))


def test_launch_agent_plist_runs_through_the_signed_app_wrapper(tmp_path):
    template = (
        ROOT / "launchd" / "com.notes-graph.mcp-remote.plist.template"
    ).read_text()
    rendered = template.replace("__HOME__", "/Users/me").replace(
        "__NOTES_GRAPH_DIR__", "/repo"
    )
    plist = plistlib.loads(rendered.encode())
    assert plist["Label"] == "com.notes-graph.mcp-remote"
    assert plist["ProgramArguments"] == [
        "/Users/me/Applications/AppleNotesMCP.app/Contents/MacOS/AppleNotesMCPRunner",
        "--bridge-stdio",
    ]
    assert (
        plist["EnvironmentVariables"]["NOTES_GRAPH_PYTHON_ENTRY"]
        == "/repo/.venv/bin/apple-notes-mcp-remote"
    )
    assert plist["KeepAlive"] is True and plist["RunAtLoad"] is True
    assert plist["StandardOutPath"].startswith(
        "/Users/me/Library/Logs/apple-notes-mcp/"
    )
    assert plist["StandardErrorPath"].startswith(
        "/Users/me/Library/Logs/apple-notes-mcp/"
    )
    assert "__" not in rendered


def test_remote_entry_defaults_to_serve():
    assert cli.build_parser().parse_args([]).command == "serve"


def test_token_store_writers_are_serialized_across_store_instances(tmp_path):
    # Separate instances stand in for the server and the revoke-all CLI process:
    # each has its own threading.Lock, so only the file lock can serialize them.
    import threading

    server_store, cli_store = TokenStore(tmp_path), TokenStore(tmp_path)
    server_store.put_access("tok", {"client_id": "c", "expires_at": 9e9, "scopes": []})
    done = threading.Event()
    with server_store._file_lock():
        worker = threading.Thread(target=lambda: (cli_store.revoke_all(), done.set()))
        worker.start()
        assert not done.wait(0.3), "revoke_all ran while another writer held the store"
    worker.join(5)
    assert done.is_set() and server_store.get_access("tok") is None
    assert not list(tmp_path.glob("*.tmp"))


def _kill_switch(tmp_path, funnel_status):
    for name in ("launchctl", "pkill"):
        _stub_bin(tmp_path, name)
    ts = tmp_path / "bin" / "tailscale"
    ts.write_text(
        '#!/bin/sh\necho "tailscale $*" >> "$STUB_LOG"\n'
        'if [ "$1 $2" = "funnel status" ]; then printf "%s\\n" "$STUB_TS_STATUS"; fi\nexit 0\n'
    )
    ts.chmod(0o755)
    log = tmp_path / "calls.log"
    env = {
        "PATH": f"{tmp_path / 'bin'}:/usr/bin:/bin",
        "STUB_LOG": str(log),
        "HOME": str(tmp_path),
        "STUB_TS_STATUS": funnel_status,
    }
    r = subprocess.run(
        [str(ROOT / "scripts" / "remote-kill-switch.sh")],
        env=env,
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr
    return log.read_text().splitlines()


def test_kill_switch_leaves_an_unrelated_funnel_alone(tmp_path):
    calls = _kill_switch(
        tmp_path,
        "https://mac.tail.ts.net (Funnel on)\n|-- / proxy http://127.0.0.1:8080",
    )
    assert not any(c.endswith(" off") for c in calls if c.startswith("tailscale"))


def test_kill_switch_turns_off_our_funnel(tmp_path):
    calls = _kill_switch(
        tmp_path,
        "https://mac.tail.ts.net (Funnel on)\n|-- / proxy http://127.0.0.1:9893",
    )
    assert "tailscale funnel --https=443 off" in calls


def test_kill_switch_port_match_is_exact(tmp_path):
    calls = _kill_switch(
        tmp_path,
        "https://mac.tail.ts.net (Funnel on)\n|-- / proxy http://127.0.0.1:98930",
    )
    assert not any(c.endswith(" off") for c in calls if c.startswith("tailscale"))


def test_registration_is_refused_when_every_slot_holds_live_grants(tmp_path):
    from apple_notes_mcp.remote.auth import MAX_CLIENTS, StoreFullError

    store = TokenStore(tmp_path)
    for i in range(MAX_CLIENTS):
        store.put_client(f"c{i}", {"client_id": f"c{i}", "client_id_issued_at": i})
        store.put_tokens(
            f"a{i}",
            {"client_id": f"c{i}", "expires_at": 9e9},
            f"r{i}",
            {"client_id": f"c{i}", "expires_at": 9e9},
            epoch=0,
        )
    try:
        store.put_client("new", {"client_id": "new", "client_id_issued_at": 999})
    except StoreFullError:
        pass
    else:
        raise AssertionError("registration accepted with every slot in use")
    assert all(store.get_client(f"c{i}") for i in range(MAX_CLIENTS))


def test_expired_grants_do_not_protect_a_client_from_eviction(tmp_path):
    from apple_notes_mcp.remote.auth import MAX_CLIENTS

    store = TokenStore(tmp_path)
    for i in range(MAX_CLIENTS):
        store.put_client(f"c{i}", {"client_id": f"c{i}", "client_id_issued_at": i})
    # grants written straight into the file, already expired (no prune since)
    data = json.loads((tmp_path / "tokens.json").read_text())
    data["refresh"] = {
        f"h{i}": {"client_id": f"c{i}", "expires_at": 1} for i in range(MAX_CLIENTS)
    }
    (tmp_path / "tokens.json").write_text(json.dumps(data))
    store.put_client("new", {"client_id": "new", "client_id_issued_at": 999})
    assert store.get_client("new") and store.get_client("c0") is None


def test_token_store_parses_the_file_once_while_it_is_unchanged(tmp_path, monkeypatch):
    from apple_notes_mcp.remote import auth

    store = TokenStore(tmp_path)
    store.put_access("tok", {"client_id": "c", "expires_at": 9e9, "scopes": []})
    calls = []
    real_loads = auth.json.loads
    monkeypatch.setattr(
        auth.json,
        "loads",
        lambda s, *a, **k: (calls.append(1), real_loads(s, *a, **k))[1],
    )
    for _ in range(5):
        assert store.get_access("tok")
    assert len(calls) <= 1
    TokenStore(tmp_path).revoke_all()  # another process rewrites the file
    assert store.get_access("tok") is None


def test_allow_client_command(tmp_path, capsys):
    config = tmp_path / "remote.json"
    config.write_text(
        '{"public_url": "https://n.example.com", "allowed_github_login": "owner",'
        f' "allowed_github_id": 1, "github_client_id": "x", "state_dir": "{tmp_path / "state"}"}}'
    )
    assert cli.main(["--config", str(config), "allow-client", "client-123"]) == 0
    assert TokenStore(tmp_path / "state").client_allowed("client-123")
    assert "client-123" in capsys.readouterr().out


def test_request_log_rotates_and_truncates_paths(tmp_path):
    from apple_notes_mcp.remote.request_log import MAX_PATH_CHARS, RequestLog

    log = RequestLog(tmp_path / "requests.log", max_bytes=2000)
    for _ in range(50):
        log.event("http", path="/x" * 500)
    assert (tmp_path / "requests.log").stat().st_size <= 2000
    assert (tmp_path / "requests.log.1").exists()
    last = json.loads((tmp_path / "requests.log").read_text().splitlines()[-1])
    assert len(last["path"]) <= MAX_PATH_CHARS


def test_remote_logging_redacts_library_records(capsys):
    import io
    import logging

    from apple_notes_mcp.remote.request_log import configure_remote_logging

    root = logging.getLogger()
    saved = (root.handlers[:], root.level)
    stream = io.StringIO()
    try:
        configure_remote_logging(stream)
        logging.getLogger("mcp.server.lowlevel.server").error(
            "Error executing tool: Note 'Secret Title' not found"
        )
        try:
            raise ValueError("Secret Title body text")
        except ValueError:
            logging.getLogger("uvicorn.error").exception(
                "Exception in ASGI application"
            )
        logging.getLogger("apple_notes_mcp.server").warning(
            "reading %s", "Secret Title"
        )
        logging.getLogger("uvicorn.error").warning("Started server process")
    finally:
        root.handlers[:], _ = saved[0], root.setLevel(saved[1])
    out = stream.getvalue()
    assert "Secret Title" not in out
    assert "ValueError" in out and "Started server process" in out


def test_serve_does_not_trust_forwarded_headers(monkeypatch):
    import uvicorn

    from apple_notes_mcp.remote import app as remote_app
    from apple_notes_mcp.remote import request_log
    from apple_notes_mcp.remote.config import RemoteConfig

    cfg = RemoteConfig(
        public_url="https://n.example.com",
        allowed_github_login="owner",
        allowed_github_id=1,
        github_client_id="x",
    )
    seen = {}
    monkeypatch.setattr(cli, "load_remote_config", lambda path: cfg)
    monkeypatch.setattr(remote_app, "build_remote_app", lambda c: "APP")
    monkeypatch.setattr(request_log, "configure_remote_logging", lambda *a: None)
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: seen.update(app=app, **kw))
    assert cli.main(["serve"]) == 0
    assert seen["app"] == "APP" and seen["host"] == "127.0.0.1"
    assert seen["proxy_headers"] is False
    assert seen["log_config"] is None


def test_reminders_records_are_redacted_in_server_log():
    import io
    import logging

    from apple_notes_mcp.remote.request_log import configure_remote_logging

    root = logging.getLogger()
    saved = (root.handlers[:], root.level)
    stream = io.StringIO()
    try:
        configure_remote_logging(stream)
        logging.getLogger("apple_notes_mcp.reminders.store").error(
            "secret title %s", "Buy ring"
        )
    finally:
        root.handlers[:], _ = saved[0], root.setLevel(saved[1])
    out = stream.getvalue()
    assert "Buy ring" not in out
    assert "details redacted" in out


CHECK_BASE = {
    "public_url": "https://connector.example.com",
    "allowed_github_login": "octo-owner",
    "allowed_github_id": 1001,
    "github_client_id": "gh-client-id",
}


def _check_config_file(tmp_path, monkeypatch, extra):
    from apple_notes_mcp.remote import config as remote_config

    path = tmp_path / "remote.json"
    path.write_text(json.dumps({**CHECK_BASE, **extra}))
    secrets = tmp_path / "remote-secrets.json"
    secrets.write_text(json.dumps({"github_client_secret": "s3cret-value"}))
    os.chmod(secrets, 0o600)
    real = remote_config.load_remote_config
    # Never read the owner's real Keychain from a test.
    monkeypatch.setattr(
        cli,
        "load_remote_config",
        lambda p, **kw: real(p, keychain_lookup=lambda: None, **kw),
    )
    return path


def test_check_config_reports_reminders_off(tmp_path, monkeypatch, capsys):
    path = _check_config_file(tmp_path, monkeypatch, {})
    called = []
    assert (
        cli._check_config(path, reminders_status=lambda: called.append(1) or "granted")
        == 0
    )
    out = capsys.readouterr().out
    assert "reminders:       off" in out
    assert "reminders_list" not in out
    assert not called


def test_check_config_reports_reminders_grant_and_hint(tmp_path, monkeypatch, capsys):
    path = _check_config_file(tmp_path, monkeypatch, {"reminders": {"enabled": True}})
    assert cli._check_config(path, reminders_status=lambda: "not-determined") == 0
    out = capsys.readouterr().out
    assert "reminders:       on, Apple Notes MCP.app access: not-determined" in out
    assert "--probe-reminders" in out
    assert "reminders_list" in out


def test_check_config_omits_hint_when_granted(tmp_path, monkeypatch, capsys):
    path = _check_config_file(tmp_path, monkeypatch, {"reminders": {"enabled": True}})
    cli._check_config(path, reminders_status=lambda: "granted")
    out = capsys.readouterr().out
    assert "access: granted" in out and "--probe-reminders" not in out


def test_check_config_read_only_line_when_no_writes(tmp_path, monkeypatch, capsys):
    path = _check_config_file(tmp_path, monkeypatch, {"reminders": {"enabled": True}})
    cli._check_config(path, reminders_status=lambda: "granted")
    out = capsys.readouterr().out
    assert "                 writes: none (read-only)" in out
    assert "never complete" not in out and "create in" not in out
    assert "reminders_complete" not in out


def test_check_config_prints_reminders_write_scope(tmp_path, monkeypatch, capsys):
    section = {
        "enabled": True,
        "never_complete_lists": ["Grocery List", "errands this week!"],
        "write_tools": ["reminders_complete", "reminders_create"],
    }
    path = _check_config_file(tmp_path, monkeypatch, {"reminders": section})
    cli._check_config(path, reminders_status=lambda: "granted")
    out = capsys.readouterr().out
    assert "                 writes: reminders_complete, reminders_create" in out
    assert "                 never complete: Grocery List, errands this week!" in out
    assert "                 create in: every list" in out
    tools_line = next(line for line in out.splitlines() if line.startswith("tools ("))
    assert "reminders_complete" in tools_line and "reminders_create" in tools_line

    section = {
        "enabled": True,
        "never_complete_lists": [],
        "create_lists": ["Professional"],
        "write_tools": ["reminders_complete", "reminders_create"],
    }
    path = _check_config_file(tmp_path, monkeypatch, {"reminders": section})
    cli._check_config(path, reminders_status=lambda: "granted")
    out = capsys.readouterr().out
    assert "never complete: (none)" in out and "create in: Professional" in out


def test_check_config_prints_where_reminders_update_may_move_to(
    tmp_path, monkeypatch, capsys
):
    """#232: a move lands only in create_lists, so remote-check names them for update too."""
    section = {
        "enabled": True,
        "create_lists": ["Professional"],
        "write_tools": ["reminders_update"],
    }
    path = _check_config_file(tmp_path, monkeypatch, {"reminders": section})
    cli._check_config(path, reminders_status=lambda: "granted")
    out = capsys.readouterr().out
    assert "                 writes: reminders_update" in out
    assert "                 move into: Professional" in out
    assert "create in" not in out and "never complete" not in out


def test_wrapper_status_reports_missing_app(tmp_path):
    from apple_notes_mcp.reminders.grant import (
        grant_instructions,
        wrapper_reminders_status,
    )

    status = wrapper_reminders_status(tmp_path / "nope.app")
    assert status == "not-installed"
    assert "./run mcp-app-install" in grant_instructions(status)


def test_wrapper_status_names_a_failed_open(tmp_path, monkeypatch):
    from apple_notes_mcp.reminders import grant

    app = tmp_path / "AppleNotesMCP.app"
    app.mkdir()
    monkeypatch.setattr(
        grant.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(
            a, 1, b"", b"LSOpenURLsWithRole() failed\n"
        ),
    )
    status = grant.wrapper_reminders_status(app)
    assert status.startswith("unknown (open exited 1")
    assert "LSOpenURLsWithRole" in status and "reinstall" not in status


def test_cli_imports_without_eventkit():
    """revoke-all / allow-client / serve without reminders must not need EventKit."""
    import sys

    code = (
        "import sys; sys.modules['EventKit'] = None; import apple_notes_mcp.remote.cli"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_check_config_points_denied_access_at_system_settings(
    tmp_path, monkeypatch, capsys
):
    path = _check_config_file(tmp_path, monkeypatch, {"reminders": {"enabled": True}})
    cli._check_config(path, reminders_status=lambda: "denied")
    out = capsys.readouterr().out
    assert "Privacy & Security → Reminders" in out
    assert "--probe-reminders" not in out


def test_grant_instructions_by_status():
    from apple_notes_mcp.reminders.grant import grant_instructions

    assert "--probe-reminders" in grant_instructions("not-determined")
    assert "--probe-reminders" in grant_instructions("unknown (old app)")
    for status in ("denied", "restricted", "write-only"):
        assert "Privacy & Security → Reminders" in grant_instructions(status)


def test_ui_helper_plist_runs_through_the_signed_app_wrapper():
    """The daemon reads NoteStore, so it needs the wrapper's Full Disk Access."""
    template = (
        ROOT / "launchd" / "com.notes-graph.ui-helper.plist.template"
    ).read_text()
    rendered = template.replace("__HOME__", "/Users/me").replace(
        "__NOTES_GRAPH_DIR__", "/repo"
    )
    plist = plistlib.loads(rendered.encode())
    assert plist["Label"] == "com.notes-graph.ui-helper"
    assert plist["ProgramArguments"] == [
        "/Users/me/Applications/AppleNotesMCP.app/Contents/MacOS/AppleNotesMCPRunner",
        "--bridge-stdio",
    ]
    assert (
        plist["EnvironmentVariables"]["NOTES_GRAPH_PYTHON_ENTRY"]
        == "/repo/.venv/bin/notes-graph-ui-helper"
    )
    assert plist["KeepAlive"] is True and plist["RunAtLoad"] is True
    assert "__" not in rendered


FAKE_LAUNCHCTL = """#!/bin/sh
echo "launchctl $*" >> "$STUB_LOG"
state=$(cat "$STUB_STATE")
case "$1" in
  bootout) [ "$STUCK_BOOTOUT" = 1 ] || echo gone > "$STUB_STATE"; exit 0 ;;
  bootstrap)
    [ "$FAIL_BOOTSTRAP" = 1 ] && exit 5
    echo "${STATE_AFTER_BOOTSTRAP:-running}" > "$STUB_STATE"; exit 0 ;;
  print)
    [ "$state" = gone ] && exit 113
    if [ "$state" = running-once ]; then
      echo "spawn scheduled" > "$STUB_STATE"
      state=running
    fi
    printf '\\tstate = %s\\n' "$state"; exit 0 ;;
esac
exit 0
"""
SERVICE = f"gui/{os.getuid()}/com.notes-graph.ui-helper"


def _ui_helper_install(tmp_path, *, wrapper=True, entry=True, stubs=(), **stub_env):
    """Run `ui-helper-install` from a throwaway checkout against a fake launchd."""
    repo = tmp_path / "repo"
    (repo / "launchd").mkdir(parents=True)
    (repo / "scripts").mkdir()
    shutil.copy2(ROOT / "run", repo / "run")
    shutil.copy2(ROOT / "scripts" / "_cli.sh", repo / "scripts")
    shutil.copy2(
        ROOT / "launchd" / "com.notes-graph.ui-helper.plist.template",
        repo / "launchd",
    )
    if entry:
        (repo / ".venv/bin").mkdir(parents=True)
        (repo / ".venv/bin/notes-graph-ui-helper").write_text("#!/bin/sh\n")
        (repo / ".venv/bin/notes-graph-ui-helper").chmod(0o755)

    for name in ("uv", "sleep", *stubs):
        _stub_bin(tmp_path, name, exit_code=1 if name in stubs else 0)
    launchctl = tmp_path / "bin" / "launchctl"
    launchctl.write_text(FAKE_LAUNCHCTL)
    launchctl.chmod(0o755)

    agents = tmp_path / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    plist = agents / "com.notes-graph.ui-helper.plist"
    plist.write_text("old definition")
    if wrapper:
        runner = tmp_path / "Applications/AppleNotesMCP.app/Contents/MacOS"
        runner.mkdir(parents=True)
        (runner / "AppleNotesMCPRunner").write_text("#!/bin/sh\n")
        (runner / "AppleNotesMCPRunner").chmod(0o755)
    state = tmp_path / "state"
    state.write_text("running")
    log = tmp_path / "calls.log"
    log.touch()
    result = subprocess.run(
        [str(repo / "run"), "ui-helper-install"],
        env={
            "PATH": f"{tmp_path / 'bin'}:/usr/bin:/bin",
            "STUB_LOG": str(log),
            "STUB_STATE": str(state),
            "HOME": str(tmp_path),
            **stub_env,
        },
        capture_output=True,
        text=True,
    )
    calls = [c for c in log.read_text().splitlines() if c.startswith("launchctl")]
    return result, calls, plist


def _changes_launchd(calls):
    return [c for c in calls if c.split()[1] != "print"]


def test_ui_helper_install_replaces_a_daemon_that_is_already_loaded(tmp_path):
    """Plain `load` leaves the old definition running and exits 0 when it fails."""
    result, calls, plist = _ui_helper_install(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _changes_launchd(calls) == [
        f"launchctl bootout {SERVICE}",
        f"launchctl bootstrap gui/{os.getuid()} {plist}",
    ]
    assert "AppleNotesMCPRunner" in plist.read_text()
    assert [p.name for p in plist.parent.iterdir()] == [plist.name]


def test_ui_helper_install_refuses_without_the_wrapper(tmp_path):
    result, calls, plist = _ui_helper_install(tmp_path, wrapper=False)
    assert result.returncode != 0
    assert "mcp-app-install" in result.stdout + result.stderr
    assert plist.read_text() == "old definition"
    assert calls == []


def test_ui_helper_install_refuses_without_the_daemon_entry(tmp_path):
    result, calls, plist = _ui_helper_install(tmp_path, entry=False)
    assert result.returncode != 0
    assert "notes-graph-ui-helper" in result.stdout + result.stderr
    assert plist.read_text() == "old definition"
    assert calls == []


def test_ui_helper_install_keeps_the_daemon_when_the_plist_is_invalid(tmp_path):
    result, calls, plist = _ui_helper_install(tmp_path, stubs=("plutil",))
    assert result.returncode != 0
    assert plist.read_text() == "old definition"
    assert calls == []
    assert [p.name for p in plist.parent.iterdir()] == [plist.name]


def test_ui_helper_install_fails_when_the_old_daemon_will_not_exit(tmp_path):
    result, calls, plist = _ui_helper_install(tmp_path, STUCK_BOOTOUT="1")
    assert result.returncode != 0
    assert "Installed" not in result.stdout
    assert not any("bootstrap" in c for c in calls)
    assert plist.read_text() == "old definition"


def test_ui_helper_install_fails_when_bootstrap_fails(tmp_path):
    result, _, _ = _ui_helper_install(tmp_path, FAIL_BOOTSTRAP="1")
    assert result.returncode != 0
    assert "Installed" not in result.stdout


def test_ui_helper_install_fails_when_the_daemon_never_runs(tmp_path):
    """A bootstrap can succeed while the job sits in launchd's retry loop."""
    result, _, _ = _ui_helper_install(tmp_path, STATE_AFTER_BOOTSTRAP="spawn scheduled")
    assert result.returncode != 0
    assert "Installed" not in result.stdout
    assert "notes-graph-ui-helper.log" in result.stdout + result.stderr


def test_ui_helper_install_fails_when_the_daemon_crashes_right_after_starting(tmp_path):
    result, _, _ = _ui_helper_install(tmp_path, STATE_AFTER_BOOTSTRAP="running-once")
    assert result.returncode != 0
    assert "Installed" not in result.stdout


def test_ui_helper_install_help_names_the_wrapper_prerequisite():
    result = subprocess.run(
        [str(ROOT / "run"), "ui-helper-install", "-h"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "mcp-app-install" in result.stdout
    assert "Device Control and Data Access" in result.stdout
