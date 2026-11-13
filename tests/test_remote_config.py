"""Remote-mode config loading + validation (issue #167)."""

import json
import os

import pytest

from apple_notes_mcp.remote.config import (
    RemoteConfigError,
    load_remote_config,
)


BASE = {
    "public_url": "https://connector.example.com",
    "allowed_github_login": "octo-owner",
    "allowed_github_id": 1001,
    "github_client_id": "gh-client-id",
}


def _write(tmp_path, data, name="remote.json"):
    path = tmp_path / name
    path.write_text(json.dumps(data))
    return path


def _secrets(tmp_path, mode=0o600):
    path = tmp_path / "remote-secrets.json"
    path.write_text(json.dumps({"github_client_secret": "s3cret-value"}))
    os.chmod(path, mode)
    return path


def _load(tmp_path, data, **kw):
    if "secrets_path" not in kw:
        kw["secrets_path"] = _secrets(tmp_path)
    kw.setdefault("keychain_lookup", lambda: None)
    return load_remote_config(_write(tmp_path, data), **kw)


def test_defaults(tmp_path):
    cfg = _load(tmp_path, BASE)
    assert cfg.host == "127.0.0.1"
    assert cfg.port == 9893
    assert cfg.write_tools == ()
    assert cfg.folder_allowlist is None
    assert cfg.github_client_secret == "s3cret-value"
    assert "https://claude.ai/api/mcp/auth_callback" in cfg.redirect_uri_allowlist
    assert cfg.access_token_ttl <= 3600


def test_secret_never_in_repr(tmp_path):
    cfg = _load(tmp_path, BASE)
    assert "s3cret-value" not in repr(cfg)


def test_keychain_secret_wins_over_file(tmp_path):
    cfg = _load(tmp_path, BASE, keychain_lookup=lambda: "from-keychain")
    assert cfg.github_client_secret == "from-keychain"


def test_group_or_world_readable_secrets_file_is_refused(tmp_path):
    with pytest.raises(RemoteConfigError, match="600"):
        _load(tmp_path, BASE, secrets_path=_secrets(tmp_path, mode=0o644))


def test_missing_secret_is_an_error_unless_not_required(tmp_path):
    missing = tmp_path / "nope.json"
    with pytest.raises(RemoteConfigError, match="secret"):
        _load(tmp_path, BASE, secrets_path=missing)
    cfg = _load(tmp_path, BASE, secrets_path=missing, require_secret=False)
    assert cfg.github_client_secret is None


def test_secret_in_main_config_is_refused(tmp_path):
    with pytest.raises(RemoteConfigError, match="secret"):
        _load(tmp_path, {**BASE, "github_client_secret": "oops"})


def test_host_cannot_be_overridden(tmp_path):
    with pytest.raises(RemoteConfigError, match="host"):
        _load(tmp_path, {**BASE, "host": "0.0.0.0"})


@pytest.mark.parametrize(
    "key", ["public_url", "allowed_github_login", "allowed_github_id", "github_client_id"]
)
def test_required_keys(tmp_path, key):
    data = dict(BASE)
    del data[key]
    with pytest.raises(RemoteConfigError, match=key):
        _load(tmp_path, data)


def test_public_url_must_be_https(tmp_path):
    with pytest.raises(RemoteConfigError, match="https"):
        _load(tmp_path, {**BASE, "public_url": "http://connector.example.com"})


def test_public_url_must_not_carry_a_path(tmp_path):
    with pytest.raises(RemoteConfigError, match="public_url"):
        _load(tmp_path, {**BASE, "public_url": "https://connector.example.com/mcp"})


@pytest.mark.parametrize(
    "name", ["export_note", "export_graph", "bogus"]
)
def test_write_tools_reject_never_remote_and_unknown(tmp_path, name):
    with pytest.raises(RemoteConfigError, match=name):
        _load(tmp_path, {**BASE, "write_tools": [name]})


def test_write_tools_opt_in(tmp_path):
    cfg = _load(tmp_path, {**BASE, "write_tools": ["toggle_checklist_item"]})
    assert cfg.write_tools == ("toggle_checklist_item",)


def test_folder_allowlist(tmp_path):
    cfg = _load(tmp_path, {**BASE, "folder_allowlist": ["Daily", "★ Maps"]})
    assert cfg.folder_allowlist == ("Daily", "★ Maps")


def test_empty_folder_allowlist_is_refused(tmp_path):
    with pytest.raises(RemoteConfigError, match="folder_allowlist"):
        _load(tmp_path, {**BASE, "folder_allowlist": []})


def test_missing_config_file_explains_where_it_goes(tmp_path):
    with pytest.raises(RemoteConfigError, match="remote.json"):
        load_remote_config(tmp_path / "remote.json", keychain_lookup=lambda: None)


def test_secrets_file_defaults_to_beside_the_config(tmp_path):
    _secrets(tmp_path)  # tmp_path/remote-secrets.json, next to remote.json
    cfg = load_remote_config(_write(tmp_path, BASE), keychain_lookup=lambda: None)
    assert cfg.github_client_secret == "s3cret-value"


def test_write_tools_cannot_combine_with_folder_allowlist(tmp_path):
    # Writes address notes by title through AppleScript, outside NotesDatabase's
    # folder scope, so a scoped server must stay read-only (security review, #167).
    with pytest.raises(RemoteConfigError, match="folder_allowlist"):
        _load(
            tmp_path,
            {**BASE, "write_tools": ["add_note"], "folder_allowlist": ["Daily"]},
        )


def test_remote_config_object_refuses_writes_with_folder_scope():
    from apple_notes_mcp.remote.config import RemoteConfig

    with pytest.raises(ValueError, match="folder_allowlist"):
        RemoteConfig(
            public_url="https://n.example.com",
            allowed_github_login="owner",
            github_client_id="x",
            write_tools=("add_note",),
            folder_allowlist=("Daily",),
        )


def test_non_numeric_port_is_a_config_error(tmp_path):
    with pytest.raises(RemoteConfigError, match="port"):
        _load(tmp_path, {**BASE, "port": "nine"})


def test_provider_fails_closed_without_a_pinned_id(tmp_path):
    from apple_notes_mcp.remote.auth import GitHubOAuthProvider, TokenStore
    from apple_notes_mcp.remote.config import RemoteConfig
    from apple_notes_mcp.remote.request_log import RequestLog

    cfg = RemoteConfig(public_url="https://n.example.com", allowed_github_login="owner", github_client_id="x")
    provider = GitHubOAuthProvider(cfg, TokenStore(tmp_path), RequestLog(None))
    assert not provider._identity_allowed({"login": "owner", "id": 1})



def test_bool_github_id_is_refused(tmp_path):
    with pytest.raises(RemoteConfigError, match="allowed_github_id"):
        _load(tmp_path, {**BASE, "allowed_github_id": True})


# --- write_queue block (issue #169) ------------------------------------------


def test_write_queue_defaults(tmp_path):
    from apple_notes_mcp.remote.write_queue import ALL_OPERATIONS, DEFAULT_STATE_DIR

    wq = _load(tmp_path, BASE).write_queue
    assert wq.state_dir == DEFAULT_STATE_DIR
    assert wq.auto_apply is True
    assert wq.apply_delay_after_unlock_seconds == 60
    assert wq.max_body_chars == 20_000 and wq.max_per_day == 100
    assert not hasattr(wq, "max_per_hour")  # removed: only the daily cap limits volume
    assert wq.allowed_operations == ALL_OPERATIONS


def test_write_queue_overrides(tmp_path):
    data = dict(
        BASE,
        write_queue={
            "state_dir": str(tmp_path / "scratch-queue"),
            "auto_apply": False,
            "apply_delay_after_unlock_seconds": 5,
            "max_per_day": 3,
            "allowed_operations": ["append", "toggle_checklist_item"],
        },
    )
    wq = _load(tmp_path, data).write_queue
    assert wq.state_dir == tmp_path / "scratch-queue"
    assert wq.auto_apply is False
    assert wq.apply_delay_after_unlock_seconds == 5
    assert wq.max_per_day == 3
    assert wq.allowed_operations == frozenset({"append", "toggle_checklist_item"})


def test_write_queue_legacy_max_per_hour_is_ignored(tmp_path):
    # Live remote.json files still carry the removed hourly cap; they must load.
    data = dict(BASE, write_queue={"max_per_hour": 80, "max_per_day": 200})
    wq = _load(tmp_path, data).write_queue
    assert wq.max_per_day == 200
    assert not hasattr(wq, "max_per_hour")


def test_write_queue_state_dir_expands_user(tmp_path):
    data = dict(BASE, write_queue={"state_dir": "~/wq-test"})
    assert str(_load(tmp_path, data).write_queue.state_dir).startswith(os.path.expanduser("~"))


@pytest.mark.parametrize(
    "block, match",
    [
        ("nope", "write_queue must be an object"),
        ({"bogus": 1}, "unknown write_queue key"),
        ({"auto_apply": "yes"}, "auto_apply must be true or false"),
        ({"max_per_day": 0}, "max_per_day must be a positive integer"),
        ({"max_body_chars": True}, "max_body_chars must be a positive integer"),
        ({"apply_delay_after_unlock_seconds": -1}, "apply_delay_after_unlock_seconds must be"),
        ({"allowed_operations": ["append", "export_note"]}, "allowed_operations"),
        ({"allowed_operations": []}, "allowed_operations"),
        ({"state_dir": 5}, "state_dir must be a path"),
        ({"state_dir": "relative/wq"}, "state_dir must be an absolute path"),
    ],
)
def test_write_queue_validation(tmp_path, block, match):
    with pytest.raises(RemoteConfigError, match=match):
        _load(tmp_path, dict(BASE, write_queue=block))


def test_reminders_disabled_by_default(tmp_path):
    assert _load(tmp_path, BASE).reminders.enabled is False


def test_reminders_enabled(tmp_path):
    cfg = _load(tmp_path, {**BASE, "reminders": {"enabled": True}})
    assert cfg.reminders.enabled is True


@pytest.mark.parametrize(
    "value, message",
    [
        ("yes", "reminders must be an object"),
        ({"enabled": "yes"}, "reminders.enabled must be true or false"),
        ({"enable": True}, "unknown key reminders.enable"),
    ],
)
def test_reminders_bad_shapes_are_named(tmp_path, value, message):
    with pytest.raises(RemoteConfigError, match=message):
        _load(tmp_path, {**BASE, "reminders": value})


# --- Phase 2: Reminders write keys (#171) -----------------------------------

GUARDED = ["Grocery List", "errands this week!"]


def _reminders(tmp_path, section):
    return _load(tmp_path, {**BASE, "reminders": section}).reminders


def test_reminders_write_keys_parse(tmp_path):
    r = _reminders(
        tmp_path,
        {
            "enabled": True,
            "never_complete_lists": GUARDED,
            "create_lists": None,
            "write_tools": ["reminders_complete", "reminders_create"],
        },
    )
    assert r.never_complete_lists == tuple(GUARDED)
    assert r.create_lists is None
    assert r.write_tools == ("reminders_complete", "reminders_create")


def test_reminders_write_defaults(tmp_path):
    r = _reminders(tmp_path, {"enabled": True})
    assert r.write_tools == ()
    assert r.never_complete_lists == ()
    assert r.create_lists is None


def test_create_lists_parse_to_tuple(tmp_path):
    r = _reminders(
        tmp_path,
        {"enabled": True, "create_lists": ["Reminders"], "write_tools": ["reminders_create"]},
    )
    assert r.create_lists == ("Reminders",)


def test_complete_requires_explicit_never_complete_lists(tmp_path):
    with pytest.raises(
        RemoteConfigError,
        match=r"reminders.never_complete_lists must be set.*\[\] to allow every list",
    ):
        _reminders(tmp_path, {"enabled": True, "write_tools": ["reminders_complete"]})
    r = _reminders(
        tmp_path,
        {"enabled": True, "write_tools": ["reminders_complete"], "never_complete_lists": []},
    )
    assert r.never_complete_lists == ()


def test_reminders_write_tools_need_enabled(tmp_path):
    with pytest.raises(
        RemoteConfigError, match="reminders.write_tools needs reminders.enabled: true"
    ):
        _reminders(tmp_path, {"enabled": False, "write_tools": ["reminders_create"]})


def test_unknown_reminders_write_tool_named(tmp_path):
    with pytest.raises(
        RemoteConfigError,
        match="reminders.write_tools: reminders_delete is not a Reminders write tool",
    ):
        _reminders(tmp_path, {"enabled": True, "write_tools": ["reminders_delete"]})


def test_reminders_write_tools_must_be_a_list(tmp_path):
    with pytest.raises(RemoteConfigError, match="reminders.write_tools must be a list"):
        _reminders(tmp_path, {"enabled": True, "write_tools": "reminders_create"})


def test_empty_create_lists_refused(tmp_path):
    with pytest.raises(
        RemoteConfigError, match=r"reminders.create_lists is empty.*null for every list"
    ):
        _reminders(tmp_path, {"enabled": True, "create_lists": []})


@pytest.mark.parametrize("key", ["never_complete_lists", "create_lists"])
@pytest.mark.parametrize("bad", [[""], [3], "Grocery List"])
def test_list_name_values_must_be_nonempty_strings(tmp_path, key, bad):
    with pytest.raises(RemoteConfigError, match=f"reminders.{key} must be a list of list names"):
        _reminders(tmp_path, {"enabled": True, key: bad})


def test_reminders_tool_in_top_level_write_tools_points_at_reminders_section(tmp_path):
    with pytest.raises(
        RemoteConfigError, match="reminders_complete goes in reminders.write_tools"
    ):
        _load(tmp_path, {**BASE, "write_tools": ["reminders_complete"]})


def test_list_names_are_stripped_so_the_blocklist_cannot_fail_open(tmp_path):
    r = _reminders(
        tmp_path,
        {
            "enabled": True,
            "never_complete_lists": [" Grocery List ", "errands this week!\n"],
            "create_lists": ["  Professional"],
            "write_tools": ["reminders_complete", "reminders_create"],
        },
    )
    assert r.never_complete_lists == ("Grocery List", "errands this week!")
    assert r.create_lists == ("Professional",)



def test_non_string_reminders_write_tools_are_named(tmp_path):
    with pytest.raises(
        RemoteConfigError, match="reminders.write_tools must be a list of tool names"
    ):
        _reminders(tmp_path, {"enabled": True, "write_tools": [{"name": "reminders_create"}]})
