"""Remote-mode configuration (issue #167).

``~/Library/Application Support/notes-graph/remote.json`` holds the non-secret
settings. The GitHub OAuth client secret never goes there: it is read from the
login Keychain (service ``notes-graph-mcp-remote``, account
``github-client-secret``) or, failing that, from ``remote-secrets.json`` next
to it, which must be chmod 600.
"""

import json
import os
import stat
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

from apple_notes.config import CONFIG_DIR

from . import tools as remote_tools
from .write_queue import ALL_OPERATIONS, WriteQueueConfig

REMOTE_CONFIG_PATH = CONFIG_DIR / "remote.json"
REMOTE_SECRETS_PATH = CONFIG_DIR / "remote-secrets.json"
DEFAULT_STATE_DIR = CONFIG_DIR / "remote"
DEFAULT_LOG_PATH = Path.home() / "Library" / "Logs" / "apple-notes-mcp" / "requests.log"
KEYCHAIN_SERVICE = "notes-graph-mcp-remote"
KEYCHAIN_ACCOUNT = "github-client-secret"

BIND_HOST = "127.0.0.1"
DEFAULT_PORT = 9893
CLAUDE_REDIRECT_URIS = (
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
)


class RemoteConfigError(Exception):
    """remote.json or the secret store is missing or invalid."""


REMINDERS_KEYS = frozenset(
    {"enabled", "never_complete_lists", "create_lists", "write_tools"}
)


@dataclass(frozen=True)
class RemindersConfig:
    """The ``reminders`` section of remote.json (issue #171). Off by default.

    ``never_complete_lists`` is a blocklist of list names (case-insensitive);
    ``create_lists`` of None means every list. Writes need ``write_tools``.
    """

    enabled: bool = False
    never_complete_lists: tuple[str, ...] = ()
    create_lists: Optional[tuple[str, ...]] = None
    write_tools: tuple[str, ...] = ()


@dataclass(frozen=True)
class RemoteConfig:
    public_url: str
    allowed_github_login: str
    github_client_id: str
    github_client_secret: Optional[str] = field(default=None, repr=False)
    allowed_github_id: Optional[int] = None
    host: str = BIND_HOST
    port: int = DEFAULT_PORT
    folder_allowlist: Optional[tuple[str, ...]] = None
    write_tools: tuple[str, ...] = ()
    write_queue: WriteQueueConfig = field(default_factory=WriteQueueConfig)
    reminders: RemindersConfig = field(default_factory=RemindersConfig)
    redirect_uri_allowlist: tuple[str, ...] = CLAUDE_REDIRECT_URIS
    state_dir: Path = DEFAULT_STATE_DIR
    log_path: Path = DEFAULT_LOG_PATH
    access_token_ttl: int = 3600
    refresh_token_ttl: int = 30 * 24 * 3600
    github_authorize_url: str = "https://github.com/login/oauth/authorize"
    github_token_url: str = "https://github.com/login/oauth/access_token"
    github_user_url: str = "https://api.github.com/user"

    def __post_init__(self):
        # Write tools address notes by title through AppleScript / UI scripting,
        # which NotesDatabase's folder scope does not cover: a scoped server
        # could write to a same-titled note outside the allowlist. Until writes
        # resolve their target through the scoped database, keep them apart.
        if self.write_tools and self.folder_allowlist is not None:
            raise ValueError(
                "write_tools cannot be combined with folder_allowlist "
                "(writes are not confined by the folder scope)"
            )

    @property
    def mcp_url(self) -> str:
        return self.public_url + "/mcp"

    @property
    def github_callback_url(self) -> str:
        return self.public_url + "/oauth/github/callback"


def keychain_secret() -> Optional[str]:
    """The GitHub client secret from the login Keychain, or None."""
    try:
        result = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-s",
                KEYCHAIN_SERVICE,
                "-a",
                KEYCHAIN_ACCOUNT,
                "-w",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    secret = result.stdout.strip()
    return secret if result.returncode == 0 and secret else None


def _file_secret(path: Path) -> Optional[str]:
    if not path.exists():
        return None
    mode = stat.S_IMODE(os.stat(path).st_mode)
    if mode & 0o077:
        raise RemoteConfigError(f"{path} must be chmod 600 (it is {oct(mode)})")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise RemoteConfigError(f"cannot read {path}: {e}") from e
    secret = data.get("github_client_secret")
    return secret or None


def _validate_public_url(url: object) -> str:
    if not isinstance(url, str):
        raise RemoteConfigError(
            "public_url must be a string like https://connector.example.com"
        )
    parsed = urlparse(url.rstrip("/"))
    if parsed.scheme != "https":
        raise RemoteConfigError(
            "public_url must be an https:// URL (Claude only connects over HTTPS)"
        )
    if not parsed.hostname or parsed.path or parsed.query or parsed.fragment:
        raise RemoteConfigError(
            "public_url must be scheme + host only, e.g. https://connector.example.com"
        )
    return url.rstrip("/")


_WQ_INT_KEYS = (
    "max_body_chars",
    "max_old_text_chars",
    "max_per_day",
    "max_pending",
    "max_pending_per_note",
    "pending_ttl_seconds",
    "purge_after_seconds",
)


def _parse_write_queue(block: object) -> WriteQueueConfig:
    """Validate the optional ``write_queue`` object (issue #169)."""
    if block is None:
        return WriteQueueConfig()
    if not isinstance(block, dict):
        raise RemoteConfigError("write_queue must be an object")
    # max_per_hour was removed (bulk Duet edits kept hitting it; max_per_day
    # still bounds volume). Accepted and ignored so existing files still load.
    known = {"state_dir", "auto_apply", "apply_delay_after_unlock_seconds", "allowed_operations", "max_per_hour", *_WQ_INT_KEYS}
    unknown = sorted(set(block) - known)
    if unknown:
        raise RemoteConfigError(f"unknown write_queue key(s): {', '.join(unknown)}")
    kwargs: dict = {}
    if "state_dir" in block:
        if not isinstance(block["state_dir"], str) or not block["state_dir"].strip():
            raise RemoteConfigError("write_queue.state_dir must be a path")
        kwargs["state_dir"] = Path(os.path.expanduser(block["state_dir"]))
        if not kwargs["state_dir"].is_absolute():
            # The LaunchAgent and ./run remote-writes run from different cwds.
            raise RemoteConfigError("write_queue.state_dir must be an absolute path (or start with ~)")
    if "auto_apply" in block:
        if not isinstance(block["auto_apply"], bool):
            raise RemoteConfigError("write_queue.auto_apply must be true or false")
        kwargs["auto_apply"] = block["auto_apply"]
    if "apply_delay_after_unlock_seconds" in block:
        value = block["apply_delay_after_unlock_seconds"]
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 3600:
            raise RemoteConfigError(
                "write_queue.apply_delay_after_unlock_seconds must be an integer from 0 to 3600"
            )
        kwargs["apply_delay_after_unlock_seconds"] = value
    for key in _WQ_INT_KEYS:
        if key in block:
            value = block[key]
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise RemoteConfigError(f"write_queue.{key} must be a positive integer")
            kwargs[key] = value
    if "allowed_operations" in block:
        ops = block["allowed_operations"]
        if (
            not isinstance(ops, list)
            or not ops
            or not all(isinstance(o, str) and o in ALL_OPERATIONS for o in ops)
        ):
            raise RemoteConfigError(
                "write_queue.allowed_operations must be a non-empty list drawn from "
                + ", ".join(sorted(ALL_OPERATIONS))
            )
        kwargs["allowed_operations"] = frozenset(ops)
    return WriteQueueConfig(**kwargs)


def _reminders_config(value: object) -> RemindersConfig:
    if value is None:
        return RemindersConfig()
    if not isinstance(value, dict):
        raise RemoteConfigError('reminders must be an object like {"enabled": true}')
    for key in value:
        if key not in REMINDERS_KEYS:
            raise RemoteConfigError(
                f"unknown key reminders.{key} in remote.json (known: {', '.join(sorted(REMINDERS_KEYS))})"
            )
    enabled = value.get("enabled", False)
    if not isinstance(enabled, bool):
        raise RemoteConfigError("reminders.enabled must be true or false")

    write_tools = value.get("write_tools", [])
    if not isinstance(write_tools, list):
        raise RemoteConfigError(
            'reminders.write_tools must be a list like ["reminders_create"]'
        )
    if not all(isinstance(name, str) for name in write_tools):
        raise RemoteConfigError(
            'reminders.write_tools must be a list of tool names, like ["reminders_create"]'
        )
    try:
        write_tools = remote_tools.validate_reminders_write_tools(write_tools)
    except ValueError as e:
        raise RemoteConfigError(f"reminders.write_tools: {e}") from e
    if write_tools and not enabled:
        raise RemoteConfigError("reminders.write_tools needs reminders.enabled: true")

    never_complete = _list_names(value, "never_complete_lists")
    if "reminders_complete" in write_tools and never_complete is None:
        raise RemoteConfigError(
            "reminders.write_tools includes reminders_complete, so "
            "reminders.never_complete_lists must be set (use [] to allow every list)"
        )
    create_lists = _list_names(value, "create_lists")
    if create_lists == ():
        raise RemoteConfigError(
            "reminders.create_lists is empty; use null for every list, or leave "
            "reminders_create and reminders_update out of reminders.write_tools"
        )
    return RemindersConfig(
        enabled=enabled,
        never_complete_lists=never_complete or (),
        create_lists=create_lists,
        write_tools=write_tools,
    )


def _list_names(section: dict, key: str) -> Optional[tuple[str, ...]]:
    """A list-name key of the reminders section; None when absent or null."""
    names = section.get(key)
    if names is None:
        return None
    if not isinstance(names, list) or not all(
        isinstance(n, str) and n.strip() for n in names
    ):
        raise RemoteConfigError(
            f'reminders.{key} must be a list of list names, like ["Grocery List"]'
        )
    # Stripped: a stray space must not quietly switch a blocklisted list off.
    return tuple(n.strip() for n in names)


def load_remote_config(
    path: Optional[Path] = None,
    *,
    secrets_path: Optional[Path] = None,
    keychain_lookup: Callable[[], Optional[str]] = keychain_secret,
    require_secret: bool = True,
) -> RemoteConfig:
    """Load and validate remote.json; raise RemoteConfigError on any problem."""
    path = Path(path) if path else REMOTE_CONFIG_PATH
    if not path.exists():
        raise RemoteConfigError(
            f"no remote config at {path} - create remote.json (see docs/remote-connector.md)"
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise RemoteConfigError(f"cannot read {path}: {e}") from e
    if not isinstance(data, dict):
        raise RemoteConfigError(f"{path} must hold a JSON object")

    if "github_client_secret" in data:
        raise RemoteConfigError(
            "github_client_secret must not be in remote.json - put it in the Keychain or remote-secrets.json"
        )
    if "host" in data:
        raise RemoteConfigError(
            f"host is fixed at {BIND_HOST}; remove it from remote.json"
        )
    for key in ("public_url", "allowed_github_login", "allowed_github_id", "github_client_id"):
        if not data.get(key):
            raise RemoteConfigError(f"remote.json is missing {key}")

    try:
        write_tools = remote_tools.validate_write_tools(data.get("write_tools", []))
    except ValueError as e:
        raise RemoteConfigError(f"write_tools: {e}") from e

    folder_allowlist = data.get("folder_allowlist")
    if folder_allowlist is not None:
        if (
            not isinstance(folder_allowlist, list)
            or not folder_allowlist
            or not all(isinstance(f, str) and f.strip() for f in folder_allowlist)
        ):
            raise RemoteConfigError(
                "folder_allowlist must be a non-empty list of folder names (omit it to allow all folders)"
            )
        folder_allowlist = tuple(folder_allowlist)

    reminders = _reminders_config(data.get("reminders"))

    allowed_id = data.get("allowed_github_id")
    if allowed_id is not None and (isinstance(allowed_id, bool) or not isinstance(allowed_id, int)):
        raise RemoteConfigError("allowed_github_id must be the numeric GitHub user id")

    secret = None
    if require_secret:
        secret = keychain_lookup() or _file_secret(
            Path(secrets_path)
            if secrets_path
            else path.parent / REMOTE_SECRETS_PATH.name
        )
    if require_secret and not secret:
        raise RemoteConfigError(
            f"no GitHub client secret: add it to the Keychain (service {KEYCHAIN_SERVICE}, "
            f"account {KEYCHAIN_ACCOUNT}) or to remote-secrets.json (chmod 600)"
        )

    kwargs = {}
    for key in ("state_dir", "log_path"):
        if data.get(key):
            kwargs[key] = Path(os.path.expanduser(data[key]))
    if "port" in data:
        if not isinstance(data["port"], int) or not 0 < data["port"] < 65536:
            raise RemoteConfigError("port must be a number between 1 and 65535")
        kwargs["port"] = data["port"]

    try:
        return RemoteConfig(
            public_url=_validate_public_url(data["public_url"]),
            allowed_github_login=data["allowed_github_login"],
            allowed_github_id=allowed_id,
            github_client_id=data["github_client_id"],
            github_client_secret=secret,
            folder_allowlist=folder_allowlist,
            write_tools=write_tools,
            write_queue=_parse_write_queue(data.get("write_queue")),
            reminders=reminders,
            **kwargs,
        )
    except ValueError as e:
        raise RemoteConfigError(str(e)) from e
