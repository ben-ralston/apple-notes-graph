"""Tests for the iCloud sync nudge module."""

from subprocess import CompletedProcess
from unittest.mock import patch

import pytest

from apple_notes.icloud_sync import nudge_icloud_sync

pytestmark = pytest.mark.macos_gui


class TestNudgeICloudSync:
    """Tests for nudge_icloud_sync."""

    @patch("apple_notes.icloud_sync.subprocess.run")
    def test_nudge_disabled_by_default(self, mock_run, monkeypatch):
        """No env var means killall is not called."""
        monkeypatch.delenv("NOTES_GRAPH_SYNC_NUDGE", raising=False)
        result = nudge_icloud_sync()
        assert result is False
        mock_run.assert_not_called()

    @patch(
        "apple_notes.icloud_sync.subprocess.run",
        return_value=CompletedProcess(args=["killall", "bird"], returncode=0),
    )
    def test_nudge_enabled(self, mock_run, monkeypatch):
        """Env var set to '1' triggers killall bird."""
        monkeypatch.setenv("NOTES_GRAPH_SYNC_NUDGE", "1")
        result = nudge_icloud_sync()
        assert result is True
        mock_run.assert_called_once_with(
            ["killall", "bird"], capture_output=True, timeout=5
        )

    @patch(
        "apple_notes.icloud_sync.subprocess.run",
        return_value=CompletedProcess(args=["killall", "bird"], returncode=1),
    )
    def test_nudge_nonzero_exit_returns_false(self, mock_run, monkeypatch):
        """Non-zero exit code (bird not running) returns False."""
        monkeypatch.setenv("NOTES_GRAPH_SYNC_NUDGE", "1")
        result = nudge_icloud_sync()
        assert result is False

    @patch("apple_notes.icloud_sync.subprocess.run", side_effect=OSError("not found"))
    def test_nudge_failure_silent(self, mock_run, monkeypatch):
        """Subprocess failure is caught and logged, no exception raised."""
        monkeypatch.setenv("NOTES_GRAPH_SYNC_NUDGE", "1")
        result = nudge_icloud_sync()
        assert result is False

    @pytest.mark.parametrize("value", ["1", "true", "yes", "TRUE", "Yes", "True"])
    @patch(
        "apple_notes.icloud_sync.subprocess.run",
        return_value=CompletedProcess(args=["killall", "bird"], returncode=0),
    )
    def test_nudge_truthy_values(self, mock_run, monkeypatch, value):
        """Various truthy values all enable the nudge."""
        monkeypatch.setenv("NOTES_GRAPH_SYNC_NUDGE", value)
        result = nudge_icloud_sync()
        assert result is True
        mock_run.assert_called_once()

    @pytest.mark.parametrize("value", ["0", "false", "no", "", "  ", "random"])
    @patch("apple_notes.icloud_sync.subprocess.run")
    def test_nudge_falsy_values(self, mock_run, monkeypatch, value):
        """Non-truthy values do not trigger the nudge."""
        monkeypatch.setenv("NOTES_GRAPH_SYNC_NUDGE", value)
        result = nudge_icloud_sync()
        assert result is False
        mock_run.assert_not_called()
