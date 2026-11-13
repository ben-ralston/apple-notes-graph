"""Guard: the Python interpreter and CI runner image must stay pinned (issue #163).

`macos-latest` rolled to an image where uv picked CPython 3.15, for which
onnxruntime (via fastembed) ships no wheel, breaking `uv sync --locked` on
every PR. A `.python-version` file pins local + CI (uv and setup-uv both read
it); pinning the runner image stops the next image change from silently
shifting the toolchain.
"""

import re
from pathlib import Path

ROOT = Path(__file__).parent.parent
CI_YML = ROOT / ".github" / "workflows" / "ci.yml"


def test_python_version_file_pins_minor_version():
    pin = (ROOT / ".python-version").read_text().strip()
    assert re.fullmatch(r"3\.\d+", pin), (
        f".python-version must be a minor pin, got {pin!r}"
    )


def test_python_pin_satisfies_requires_python():
    pin = (ROOT / ".python-version").read_text().strip()
    requires = re.search(
        r'requires-python\s*=\s*">=3\.(\d+)', (ROOT / "pyproject.toml").read_text()
    )
    assert requires, "pyproject.toml requires-python format changed"
    assert int(pin.split(".")[1]) >= int(requires.group(1))


def test_ci_runners_are_not_floating_latest():
    runs_on = re.findall(r"runs-on:\s*(\S+)", CI_YML.read_text())
    assert runs_on, "ci.yml has no runs-on"
    for label in runs_on:
        assert "latest" not in label, f"pin the runner image, not {label!r}"
