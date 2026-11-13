"""Daemon cleanup must not remove what a newer daemon owns (CI-visible)."""

import os

import pytest

from apple_notes import ui_helper_daemon
from apple_notes.ui_helper_daemon import UIHelperDaemon


@pytest.fixture
def paths(tmp_path, monkeypatch):
    pid_file = tmp_path / "ui-helper.pid"
    monkeypatch.setattr(ui_helper_daemon, "PID_FILE", pid_file)
    socket = tmp_path / "ui-helper.sock"
    socket.touch()
    return socket, pid_file


def test_cleanup_removes_its_own_socket_and_pid_file(paths):
    socket, pid_file = paths
    pid_file.write_text(str(os.getpid()))
    UIHelperDaemon(socket_path=socket)._cleanup()
    assert not socket.exists()
    assert not pid_file.exists()


def test_cleanup_leaves_a_newer_daemons_socket_and_pid_file(paths):
    """launchd can kill the wrapper mid-write, orphaning the old Python child.

    It finishes after a replacement daemon has started on the same paths.
    """
    socket, pid_file = paths
    pid_file.write_text(str(os.getpid() + 1))
    UIHelperDaemon(socket_path=socket)._cleanup()
    assert socket.exists()
    assert pid_file.read_text() == str(os.getpid() + 1)


def test_cleanup_is_safe_to_run_twice(paths):
    socket, pid_file = paths
    pid_file.write_text(str(os.getpid()))
    daemon = UIHelperDaemon(socket_path=socket)
    daemon._cleanup()
    daemon._cleanup()
    assert not socket.exists()


def test_cleanup_leaves_the_socket_when_the_pid_file_is_gone(paths):
    """Our PID file is already removed, so the socket may be a newer daemon's."""
    socket, _ = paths
    UIHelperDaemon(socket_path=socket)._cleanup()
    assert socket.exists()


def test_tests_never_use_the_live_daemons_pid_file():
    """A test daemon's cleanup would delete the running daemon's PID file."""
    assert ui_helper_daemon.PID_FILE != ui_helper_daemon.CONFIG_DIR / "ui-helper.pid"
