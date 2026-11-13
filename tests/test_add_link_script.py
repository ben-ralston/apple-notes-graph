"""Contract tests for scripts/add_link.py (IPC: errors as JSON, exit 0)."""

import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).parent.parent / "scripts" / "add_link.py"


def run_script(*args):
    result = subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        timeout=60,
    )
    return result.returncode, json.loads(result.stdout)


def test_missing_args_is_error_json():
    code, payload = run_script()
    assert code == 0
    assert "error" in payload


def test_missing_target_is_error_json():
    code, payload = run_script("--pk", "1")
    assert code == 0
    assert "error" in payload


def test_bogus_pk_is_error_json():
    code, payload = run_script("--pk", "999999999", "--target", "Nope")
    assert code == 0
    assert "error" in payload


def test_target_with_wiki_link_tokens_is_refused():
    # "]]" in a title would break the [[...]] markdown; refused before any
    # DB access, so this is deterministic on CI too.
    code, payload = run_script("--pk", "1", "--target", "bad]]name")
    assert code == 0
    assert "error" in payload
    assert "invalid" in payload["error"].lower()
