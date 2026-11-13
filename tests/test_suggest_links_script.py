"""Contract tests for scripts/suggest_links.py (IPC: errors as JSON, exit 0)."""

import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).parent.parent / "scripts" / "suggest_links.py"


def run_script(*args):
    result = subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        timeout=120,
    )
    return result.returncode, json.loads(result.stdout)


def test_missing_pk_is_error_json():
    code, payload = run_script()
    assert code == 0
    assert "error" in payload


def test_bogus_pk_is_error_json():
    code, payload = run_script("--pk", "999999999")
    assert code == 0
    assert "error" in payload
