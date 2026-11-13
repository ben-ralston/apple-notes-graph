"""Smoke tests for the AppleNotesMCP.app spike (issue #84).

These exercise the build artifact's structural contract and CLI surface — not
FDA propagation, which can only be validated manually after granting FDA in
System Settings to the installed bundle. See docs in the runner's --help.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DIST_APP = REPO_ROOT / "dist" / "AppleNotesMCP.app"
BUILD_SCRIPT = REPO_ROOT / "apple_notes_mcp_app" / "build.sh"


def _build_if_needed() -> Path:
    """Build the .app once for the test session; return its path."""
    if not DIST_APP.exists():
        subprocess.run(
            [str(BUILD_SCRIPT)],
            check=True,
            capture_output=True,
            cwd=REPO_ROOT,
        )
    if not DIST_APP.exists():
        pytest.skip("AppleNotesMCP.app not built and build.sh did not produce it")
    return DIST_APP


@pytest.fixture(scope="module")
def app_bundle() -> Path:
    if shutil.which("swiftc") is None:
        pytest.skip("swiftc not available")
    return _build_if_needed()


@pytest.fixture(scope="module")
def runner_executable(app_bundle: Path) -> Path:
    exe = app_bundle / "Contents" / "MacOS" / "AppleNotesMCPRunner"
    assert exe.exists(), f"Runner missing at {exe}"
    return exe


def test_bundle_layout(app_bundle: Path) -> None:
    """Bundle has the expected Mach-O + Info.plist structure."""
    assert (app_bundle / "Contents" / "MacOS" / "AppleNotesMCPRunner").exists()
    assert (app_bundle / "Contents" / "Info.plist").exists()


def test_bundle_identifier(app_bundle: Path) -> None:
    """Bundle ID is stable — TCC keys grants by this ID."""
    info = plistlib.loads((app_bundle / "Contents" / "Info.plist").read_bytes())
    assert info["CFBundleIdentifier"] == "com.notes-graph.apple-notes-mcp"
    assert info["CFBundleExecutable"] == "AppleNotesMCPRunner"
    # LSUIElement true keeps the runner out of the Dock when launched directly.
    assert info["LSUIElement"] is True


def test_bundle_codesign_verifies(app_bundle: Path) -> None:
    """codesign --verify passes (ad-hoc or signed identity, both must verify)."""
    result = subprocess.run(
        ["codesign", "--verify", "--strict", str(app_bundle)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"codesign --verify failed: {result.stderr}"


def test_runner_missing_arg_exits_2(runner_executable: Path) -> None:
    """No args -> usage on stderr, exit 2."""
    result = subprocess.run([str(runner_executable)], capture_output=True, text=True)
    assert result.returncode == 2
    assert "Usage" in result.stderr


def test_runner_unknown_arg_exits_2(runner_executable: Path) -> None:
    """Unknown subcommand -> error on stderr, exit 2."""
    result = subprocess.run(
        [str(runner_executable), "--definitely-not-a-mode"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "Unknown argument" in result.stderr


def test_probe_uv_venv_without_entry_fails(
    runner_executable: Path, tmp_path: Path
) -> None:
    """Without NOTES_GRAPH_PYTHON_ENTRY (and no installed entry file), probe fails clean."""
    # Override HOME so the App Support fallback path resolves under tmp_path
    # instead of the developer's real install, which would otherwise skip.
    env = {k: v for k, v in os.environ.items() if k != "NOTES_GRAPH_PYTHON_ENTRY"}
    env["HOME"] = str(tmp_path)
    result = subprocess.run(
        [str(runner_executable), "--probe-uv-venv"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 1
    assert "could not resolve" in result.stderr.lower()


def _write_executable(path: Path, content: str) -> Path:
    path.write_text(content)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


def test_runner_bridges_stdio_roundtrips_bytes(
    runner_executable: Path, tmp_path: Path
) -> None:
    """bridge-stdio spawns the entry, pipes stdin->child and child->stdout byte-for-byte."""
    # Fake "MCP server": cat echoes stdin to stdout, passing everything through.
    fake_server = _write_executable(
        tmp_path / "fake-server.sh",
        "#!/bin/sh\nexec cat\n",
    )

    # Binary payload with NUL, high bytes, and a trailing newline (typical JSON-RPC framing).
    payload = b'{"jsonrpc":"2.0","id":1,"method":"ping"}\n\x00\xff\xfe\n'

    result = subprocess.run(
        [str(runner_executable), "--bridge-stdio"],
        input=payload,
        capture_output=True,
        env={**os.environ, "NOTES_GRAPH_PYTHON_ENTRY": str(fake_server)},
        timeout=5,
    )
    assert result.returncode == 0, (
        f"runner exited {result.returncode}: {result.stderr!r}"
    )
    assert result.stdout == payload


def test_runner_bridges_stdio_propagates_exit_code(
    runner_executable: Path, tmp_path: Path
) -> None:
    """Child exit code flows up through the bridge."""
    failing = _write_executable(
        tmp_path / "exit-42.sh",
        "#!/bin/sh\nexit 42\n",
    )
    result = subprocess.run(
        [str(runner_executable), "--bridge-stdio"],
        input=b"",
        capture_output=True,
        env={**os.environ, "NOTES_GRAPH_PYTHON_ENTRY": str(failing)},
        timeout=5,
    )
    assert result.returncode == 42


def test_resolve_python_entry_env_var_wins_over_bundle_resource(
    app_bundle: Path, tmp_path: Path
) -> None:
    """When env var and bundle resource both point at scripts, env var takes priority."""
    # Work on a copy of the bundle — writing into Contents/Resources of the
    # shared dist/ bundle invalidates its code signature for later tests.
    bundle_copy = tmp_path / "TestApp.app"
    shutil.copytree(app_bundle, bundle_copy, symlinks=True)
    runner = bundle_copy / "Contents" / "MacOS" / "AppleNotesMCPRunner"

    bundle_target = _write_executable(
        tmp_path / "bundle-entry.sh",
        "#!/bin/sh\nexit 10\n",
    )
    env_target = _write_executable(
        tmp_path / "env-entry.sh",
        "#!/bin/sh\nexit 20\n",
    )
    resources = bundle_copy / "Contents" / "Resources"
    resources.mkdir(exist_ok=True)
    (resources / "python-entry.txt").write_text(str(bundle_target))

    result = subprocess.run(
        [str(runner), "--bridge-stdio"],
        input=b"",
        capture_output=True,
        env={**os.environ, "NOTES_GRAPH_PYTHON_ENTRY": str(env_target)},
        timeout=5,
    )
    assert result.returncode == 20, (
        f"env target should win and exit 20, got {result.returncode}"
    )


def test_bridge_stdio_without_entry_fails(
    runner_executable: Path, tmp_path: Path
) -> None:
    """bridge-stdio without a python entry exits 1 with a clear message."""
    env = {k: v for k, v in os.environ.items() if k != "NOTES_GRAPH_PYTHON_ENTRY"}
    # Override HOME so the App Support fallback resolves under tmp_path — otherwise
    # a developer box with a real install silently skips this test.
    env["HOME"] = str(tmp_path)
    result = subprocess.run(
        [str(runner_executable), "--bridge-stdio"],
        capture_output=True,
        text=True,
        env=env,
        input="",
    )
    assert result.returncode == 1
    assert "no python entry" in result.stderr.lower()


REMINDER_STATES = {"granted", "not-determined", "denied", "restricted", "write-only"}


def test_info_plist_declares_reminders_usage(app_bundle: Path) -> None:
    """Without these keys TCC refuses Reminders access outright (#171)."""
    info = plistlib.loads((app_bundle / "Contents" / "Info.plist").read_bytes())
    for key in ("NSRemindersUsageDescription", "NSRemindersFullAccessUsageDescription"):
        assert "read your reminders" in info[key]


def test_bundle_entitles_calendar_and_reminders_access(app_bundle: Path) -> None:
    """Hardened runtime blocks EventKit unless this entitlement is signed in (#171)."""
    result = subprocess.run(
        ["codesign", "-d", "--entitlements", "-", "--xml", str(app_bundle)],
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    entitlements = plistlib.loads(result.stdout)
    assert entitlements.get("com.apple.security.personal-information.calendars") is True


def test_install_resign_keeps_entitlements() -> None:
    """`./run mcp-app-install` re-signs the installed bundle; it must not drop them."""
    run_script = (REPO_ROOT / "run").read_text()
    resigns = [
        line for line in run_script.splitlines() if "codesign --sign" in line
    ]
    assert resigns and all("--entitlements" in line for line in resigns)


def test_reminders_status_prints_a_known_state(runner_executable: Path) -> None:
    """Check only: prints one word, exit 0 iff granted, never prompts."""
    r = subprocess.run(
        [str(runner_executable), "--reminders-status"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    word = r.stdout.strip()
    assert word in REMINDER_STATES
    assert r.returncode == (0 if word == "granted" else 1)
