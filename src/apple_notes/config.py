"""
Configuration management for Notes Graph.

Loads folder exclusion/inclusion settings from a shared config file
that can be used by both the Electron UI and MCP server.
"""
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

CONFIG_DIR = Path.home() / "Library" / "Application Support" / "notes-graph"
CONFIG_PATH = CONFIG_DIR / "config.json"

DEFAULT_CONFIG: dict = {
    "exclude_folders": [],
    "include_folders": [],
}


def load_config(config_path: str | None = None) -> dict:
    """
    Load configuration from file.

    Returns defaults on any error (missing file, malformed JSON, permissions).
    This function never raises exceptions - it always returns a valid config.

    Args:
        config_path: Optional explicit path to config file. If None, uses
                     ~/Library/Application Support/notes-graph/config.json

    Returns:
        Dictionary with 'exclude_folders' and 'include_folders' lists
    """
    path = Path(config_path) if config_path else CONFIG_PATH

    try:
        if path.exists():
            with open(path, encoding="utf-8") as f:
                loaded = json.load(f)

            # Merge with defaults - only accept known keys with valid types
            config = DEFAULT_CONFIG.copy()
            for key in DEFAULT_CONFIG:
                if key in loaded and isinstance(loaded[key], list):
                    # Only accept string elements, filter out invalid types
                    config[key] = [item for item in loaded[key] if isinstance(item, str)]

            return config

    except json.JSONDecodeError as e:
        logger.warning(f"Malformed config file at {path}: {e}")
    except PermissionError as e:
        logger.warning(f"Permission denied reading config at {path}: {e}")
    except OSError as e:
        logger.warning(f"Error reading config at {path}: {e}")

    return DEFAULT_CONFIG.copy()


def get_effective_filters(
    exclude_folders: list[str] | None = None,
    include_folders: list[str] | None = None,
    include_all: bool = False,
    config_path: str | None = None,
) -> tuple[list[str] | None, list[str] | None]:
    """
    Get effective filter values, merging parameters with config defaults.

    Filtering behavior:
    - Config exclusions ALWAYS apply (unless include_all=True)
    - Explicit exclude_folders ADDS to config exclusions
    - Explicit include_folders OVERRIDE the config exclusions they match
      (case-insensitive substring, like the filters themselves)
    - include_all=True bypasses ALL filtering

    Examples:
        # Config has exclude_folders=["Archive", "Deleted"]

        get_effective_filters()
        # Returns (["Archive", "Deleted"], None) - config exclusions apply

        get_effective_filters(exclude_folders=["Projects"])
        # Returns (["Archive", "Deleted", "Projects"], None) - merged

        get_effective_filters(include_folders=["Archive"])
        # Returns (["Deleted"], ["Archive"]) - Archive removed from exclusions

        # Config has exclude_folders=["🗄️ Archive"]: an include lifts any
        # config exclusion it is a (case-insensitive) substring of
        get_effective_filters(include_folders=["archive"])
        # Returns (None, ["archive"])

        get_effective_filters(include_all=True)
        # Returns (None, None) - no filtering

    Args:
        exclude_folders: Additional patterns to exclude (merged with config)
        include_folders: Patterns to include (can override exclusions)
        include_all: If True, bypass all filtering
        config_path: Optional explicit path to config file

    Returns:
        Tuple of (effective_exclude_folders, effective_include_folders)
    """
    if include_all:
        return None, None

    config = load_config(config_path)

    # Start with config values
    config_excludes = config.get("exclude_folders", [])
    config_includes = config.get("include_folders", [])

    # Merge explicit excludes with config (additive)
    if exclude_folders:
        # Combine and deduplicate
        all_excludes = list(set(config_excludes + exclude_folders))
    else:
        all_excludes = list(config_excludes) if config_excludes else []

    # Merge explicit includes with config
    if include_folders:
        all_includes = list(set(config_includes + include_folders))
    else:
        all_includes = list(config_includes) if config_includes else []

    # An explicit include lifts every CONFIG exclusion it matches under the
    # filters' own rule (case-insensitive substring, either way round, issue
    # #231), so include_folders=["Archive"] lifts "🗄️ Archive". An exclusion the caller
    # passed in this same call is lifted only by an equal pattern (even when
    # the config lists it too), so "every Work folder except Work/Archive"
    # keeps working.
    if include_folders:
        include_lower = [p.lower() for p in include_folders if p]
        explicit = set(exclude_folders or [])

        def lifted(pattern: str) -> bool:
            low = pattern.lower()
            if pattern in explicit:
                return low in include_lower
            # Either way round: "Archive" lifts "🗄️ Archive", and a more
            # specific "Other/Archive" lifts a broader "Archive" (else every
            # note the include admits would be excluded again).
            return any(inc in low or low in inc for inc in include_lower)

        all_excludes = [e for e in all_excludes if not lifted(e)]

    # Return None for empty lists to signal "no filtering"
    effective_exclude = all_excludes if all_excludes else None
    effective_include = all_includes if all_includes else None

    return effective_exclude, effective_include
