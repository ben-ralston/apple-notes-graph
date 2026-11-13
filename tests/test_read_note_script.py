"""read_note.py CLI: JSON contract for the inspector preview."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

SCRIPT = Path(__file__).parent.parent / "scripts" / "read_note.py"


def run_script(*args):
    result = subprocess.run(
        [sys.executable, str(SCRIPT), *args], capture_output=True, text=True
    )
    return result.returncode, json.loads(result.stdout)


def test_missing_name_arg_is_error_json():
    code, data = run_script()
    assert code == 0
    assert "error" in data


def test_nonexistent_note_is_error_json():
    code, data = run_script("--name", "NONEXISTENT-NOTE-XYZ-12345")
    assert code == 0
    assert "error" in data


def test_missing_both_name_and_pk_is_error_json():
    code, data = run_script()
    assert code == 0
    assert "error" in data


def test_both_name_and_pk_is_error_json():
    code, data = run_script("--name", "Whatever", "--pk", "1")
    assert code == 0
    assert "error" in data


def test_pk_happy_path_shape():
    # No DB fixture available in this environment; tolerate {"error": ...}
    # when there's no Notes database, but assert the JSON contract shape.
    code, data = run_script("--pk", "1")
    assert code == 0
    if "error" not in data:
        assert set(data.keys()) == {"name", "folder", "modified", "content"}


def test_nonexistent_pk_is_error_json():
    code, data = run_script("--pk", "999999999")
    assert code == 0
    assert "error" in data


def test_pk_path_returns_full_content_untruncated(monkeypatch, capsys):
    """The inspector reads whole notes now; 46 real notes run past 4,000 chars."""
    spec = importlib.util.spec_from_file_location("read_note_script", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    body = "x" * 20000

    class FakeDatabase:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get_note_by_pk(self, pk):
            return SimpleNamespace(pk=pk)

    class FakeReader:
        def get_note_content_for_note(self, note):
            return SimpleNamespace(
                name="Long note",
                folder="Notes",
                modified="2026-11-03",
                resolved_text=body,
            )

    monkeypatch.setattr("apple_notes.database.NotesDatabase", FakeDatabase)
    monkeypatch.setattr("apple_notes.note_content.NoteContentReader", FakeReader)
    monkeypatch.setattr(sys, "argv", ["read_note.py", "--pk", "1"])

    module.main()

    assert json.loads(capsys.readouterr().out)["content"] == body
