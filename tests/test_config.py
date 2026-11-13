"""Tests for the configuration module."""
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from apple_notes.config import (
    load_config,
    get_effective_filters,
    DEFAULT_CONFIG,
)


class TestLoadConfig:
    """Tests for the load_config function."""

    def test_returns_defaults_when_no_file(self, tmp_path):
        """Should return default config when file doesn't exist."""
        config_path = tmp_path / "nonexistent.json"
        result = load_config(str(config_path))
        assert result == DEFAULT_CONFIG

    def test_loads_valid_config(self, tmp_path):
        """Should load and return valid config from file."""
        config_path = tmp_path / "config.json"
        config_data = {
            "exclude_folders": ["Archive", "Trash"],
            "include_folders": ["Work"],
        }
        config_path.write_text(json.dumps(config_data))

        result = load_config(str(config_path))

        assert result["exclude_folders"] == ["Archive", "Trash"]
        assert result["include_folders"] == ["Work"]

    def test_merges_with_defaults(self, tmp_path):
        """Should merge loaded config with defaults for missing keys."""
        config_path = tmp_path / "config.json"
        config_data = {"exclude_folders": ["Archive"]}
        config_path.write_text(json.dumps(config_data))

        result = load_config(str(config_path))

        assert result["exclude_folders"] == ["Archive"]
        assert result["include_folders"] == []  # From defaults

    def test_ignores_unknown_keys(self, tmp_path):
        """Should ignore unknown keys in config file."""
        config_path = tmp_path / "config.json"
        config_data = {
            "exclude_folders": ["Archive"],
            "unknown_key": "value",
            "another_unknown": [1, 2, 3],
        }
        config_path.write_text(json.dumps(config_data))

        result = load_config(str(config_path))

        assert "unknown_key" not in result
        assert "another_unknown" not in result
        assert result["exclude_folders"] == ["Archive"]

    def test_handles_malformed_json(self, tmp_path):
        """Should return defaults when JSON is malformed."""
        config_path = tmp_path / "config.json"
        config_path.write_text("not valid json {")

        result = load_config(str(config_path))

        assert result == DEFAULT_CONFIG

    def test_handles_invalid_types(self, tmp_path):
        """Should use defaults when values have wrong types."""
        config_path = tmp_path / "config.json"
        config_data = {
            "exclude_folders": "should be a list",
            "include_folders": 123,
        }
        config_path.write_text(json.dumps(config_data))

        result = load_config(str(config_path))

        # Non-list values should be ignored, defaults used
        assert result["exclude_folders"] == []
        assert result["include_folders"] == []

    def test_filters_non_string_list_elements(self, tmp_path):
        """Should filter out non-string elements from lists."""
        config_path = tmp_path / "config.json"
        config_data = {
            "exclude_folders": ["Archive", 123, None, "Trash", {"nested": "object"}],
            "include_folders": ["Work", True, "Personal"],
        }
        config_path.write_text(json.dumps(config_data))

        result = load_config(str(config_path))

        # Only string elements should be kept
        assert result["exclude_folders"] == ["Archive", "Trash"]
        assert result["include_folders"] == ["Work", "Personal"]

    def test_uses_default_path_when_none(self):
        """Should use default path when no path is provided."""
        # This is tricky to test directly since it uses the default path
        # We'll just verify it doesn't crash and returns valid config
        result = load_config(None)
        assert isinstance(result, dict)
        assert "exclude_folders" in result
        assert "include_folders" in result


class TestGetEffectiveFilters:
    """Tests for the get_effective_filters function."""

    def test_include_all_bypasses_filtering(self, tmp_path):
        """Should return (None, None) when include_all is True."""
        config_path = tmp_path / "config.json"
        config_data = {"exclude_folders": ["Archive"]}
        config_path.write_text(json.dumps(config_data))

        exclude, include = get_effective_filters(
            include_all=True,
            config_path=str(config_path),
        )

        assert exclude is None
        assert include is None

    def test_config_exclusions_always_apply(self, tmp_path):
        """Config exclusions should always apply by default."""
        config_path = tmp_path / "config.json"
        config_data = {"exclude_folders": ["Archive", "Deleted"]}
        config_path.write_text(json.dumps(config_data))

        exclude, include = get_effective_filters(
            config_path=str(config_path),
        )

        assert set(exclude) == {"Archive", "Deleted"}
        assert include is None

    def test_exclude_folders_adds_to_config(self, tmp_path):
        """Explicit exclude_folders should merge with config exclusions."""
        config_path = tmp_path / "config.json"
        config_data = {"exclude_folders": ["Archive"]}
        config_path.write_text(json.dumps(config_data))

        exclude, include = get_effective_filters(
            exclude_folders=["Projects"],
            config_path=str(config_path),
        )

        # Should contain both config and explicit exclusions
        assert set(exclude) == {"Archive", "Projects"}

    def test_include_folders_overrides_exclusions(self, tmp_path):
        """Explicit include_folders should remove matching patterns from exclusions."""
        config_path = tmp_path / "config.json"
        config_data = {"exclude_folders": ["Archive", "Deleted"]}
        config_path.write_text(json.dumps(config_data))

        exclude, include = get_effective_filters(
            include_folders=["Archive"],
            config_path=str(config_path),
        )

        # Archive should be removed from exclusions since it's explicitly included
        assert exclude == ["Deleted"]
        assert include == ["Archive"]

    def test_include_case_insensitive_override(self, tmp_path):
        """Include override should work case-insensitively."""
        config_path = tmp_path / "config.json"
        config_data = {"exclude_folders": ["ARCHIVE", "Deleted"]}
        config_path.write_text(json.dumps(config_data))

        exclude, include = get_effective_filters(
            include_folders=["archive"],  # lowercase
            config_path=str(config_path),
        )

        # ARCHIVE should be removed (case-insensitive match)
        assert exclude == ["Deleted"]
        assert include == ["archive"]

    def test_empty_list_does_not_clear_config(self, tmp_path):
        """Empty exclude list should not clear config exclusions."""
        config_path = tmp_path / "config.json"
        config_data = {"exclude_folders": ["Archive"]}
        config_path.write_text(json.dumps(config_data))

        exclude, include = get_effective_filters(
            exclude_folders=[],  # Empty, but config still applies
            config_path=str(config_path),
        )

        # Config exclusions still apply
        assert exclude == ["Archive"]

    def test_mixed_params_and_config(self, tmp_path):
        """Should merge parameter and config values appropriately."""
        config_path = tmp_path / "config.json"
        config_data = {
            "exclude_folders": ["ConfigExclude"],
            "include_folders": ["ConfigInclude"],
        }
        config_path.write_text(json.dumps(config_data))

        exclude, include = get_effective_filters(
            exclude_folders=["ParamExclude"],  # Added to config
            include_folders=None,  # Use config
            config_path=str(config_path),
        )

        # Exclusions are merged
        assert set(exclude) == {"ConfigExclude", "ParamExclude"}
        # Config includes still apply
        assert include == ["ConfigInclude"]

    def test_deduplication(self, tmp_path):
        """Should deduplicate when same pattern appears in config and params."""
        config_path = tmp_path / "config.json"
        config_data = {"exclude_folders": ["Archive", "Deleted"]}
        config_path.write_text(json.dumps(config_data))

        exclude, include = get_effective_filters(
            exclude_folders=["Archive", "Projects"],  # Archive is duplicate
            config_path=str(config_path),
        )

        # Should be deduplicated
        assert set(exclude) == {"Archive", "Deleted", "Projects"}
        assert len(exclude) == 3  # No duplicates


class TestIncludeLiftsMatchingExclusions:
    """Issue #231: an include pattern lifts any config exclusion it matches
    under the filters' own substring rule, so emoji-prefixed folder names
    ("🗄️ Archive") can be shown with include_folders=["Archive"]."""

    REAL_EXCLUDES = [
        "🗄️ Archive",
        "🧪 Drafts",
        "⚙️ Reference Tables",
        "Recently Deleted",
        "★ Daily Tasks",
    ]

    @pytest.fixture
    def config_path(self, tmp_path):
        path = tmp_path / "config.json"
        path.write_text(json.dumps({"exclude_folders": self.REAL_EXCLUDES}))
        return str(path)

    @staticmethod
    def _notes():
        from types import SimpleNamespace

        folders = [
            "Notes",
            "Other/🗄️ Archive",
            "Other/🧪 Drafts",
            "Other/⚙️ Reference Tables",
            "Recently Deleted",
        ]
        return [SimpleNamespace(name=f"in {f}", folder=f) for f in folders]

    def _visible(self, config_path, **kwargs):
        from apple_notes.filters import filter_notes

        exclude, include = get_effective_filters(config_path=config_path, **kwargs)
        return {n.folder for n in filter_notes(self._notes(), exclude, include)}

    def test_archive_include_shows_emoji_archive(self, config_path):
        assert self._visible(config_path, include_folders=["Archive"]) == {
            "Other/🗄️ Archive"
        }

    def test_drafts_include_shows_emoji_drafts(self, config_path):
        assert self._visible(config_path, include_folders=["drafts"]) == {
            "Other/🧪 Drafts"
        }

    def test_other_exclusions_still_apply(self, config_path):
        exclude, _ = get_effective_filters(
            include_folders=["Archive"], config_path=config_path
        )
        assert "🗄️ Archive" not in exclude
        assert set(exclude) == set(self.REAL_EXCLUDES) - {"🗄️ Archive"}
        # Including the parent folder shows its plain notes, not the
        # excluded subfolders the include pattern doesn't name.
        assert self._visible(config_path, include_folders=["Other"]) == set()

    def test_default_call_still_hides_archive_and_drafts(self, config_path):
        visible = self._visible(config_path)
        assert visible == {"Notes"}

    def test_explicit_excludes_are_not_lifted_by_substring(self, config_path):
        # "every Work folder except Work/Archive" keeps working.
        exclude, include = get_effective_filters(
            include_folders=["Work"],
            exclude_folders=["Work/Archive"],
            config_path=config_path,
        )
        assert "Work/Archive" in exclude
        assert include == ["Work"]

    def test_explicit_exclude_wins_even_when_also_configured(self, tmp_path):
        # Review (Codex): an exclusion the caller passes stays, even if the
        # config lists the same pattern and an include is a substring of it.
        path = tmp_path / "config.json"
        path.write_text(json.dumps({"exclude_folders": ["Work/Archive"]}))
        exclude, _ = get_effective_filters(
            include_folders=["Work"],
            exclude_folders=["Work/Archive"],
            config_path=str(path),
        )
        assert exclude == ["Work/Archive"]

    def test_more_specific_include_lifts_a_broader_exclusion(self, tmp_path):
        # Review: config "Archive" + include "Other/Archive" used to return
        # nothing (every included note was also excluded).
        from types import SimpleNamespace

        from apple_notes.filters import filter_notes

        path = tmp_path / "config.json"
        path.write_text(json.dumps({"exclude_folders": ["Archive", "Drafts"]}))
        exclude, include = get_effective_filters(
            include_folders=["Other/Archive"], config_path=str(path)
        )
        assert exclude == ["Drafts"]
        notes = [SimpleNamespace(folder=f) for f in ("Other/Archive", "Work/Archive", "Notes")]
        assert [n.folder for n in filter_notes(notes, exclude, include)] == ["Other/Archive"]
