"""Contract tests for scripts/delete_note.py (IPC: errors as JSON, exit 0)."""

import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).parent.parent / "scripts" / "delete_note.py"


def run_script(*args):
    result = subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        timeout=60,
    )
    return result.returncode, json.loads(result.stdout)


def test_missing_pk_is_error_json():
    code, payload = run_script()
    assert code == 0
    assert "error" in payload


def test_non_numeric_pk_is_error_json():
    # argparse rejects a non-int --pk; the script must still emit JSON, not a
    # usage message on stderr with a non-zero exit.
    code, payload = run_script("--pk", "abc")
    assert code == 0
    assert "error" in payload


def test_bogus_pk_is_error_json():
    code, payload = run_script("--pk", "999999999")
    assert code == 0
    assert "error" in payload
