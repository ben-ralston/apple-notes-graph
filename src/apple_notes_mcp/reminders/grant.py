"""Whether the AppleNotesMCP.app wrapper holds Reminders access (issue #171).

Asking from this process would answer for the terminal that runs it, not for
the wrapper the LaunchAgent runs. ``open`` launches the app through
LaunchServices, which makes it its own responsible process, so its
``--reminders-status`` answer is the wrapper's grant.
"""

import subprocess
import tempfile
from pathlib import Path

from apple_notes import demo_mode

WRAPPER_APP = Path.home() / "Applications" / "AppleNotesMCP.app"
GRANT_HINT = "open ~/Applications/AppleNotesMCP.app --args --probe-reminders"
SETTINGS_HINT = (
    'turn on "Apple Notes MCP" in System Settings → Privacy & Security → Reminders'
)
# EKAuthorizationStatus raw values. The Swift wrapper prints the same words.
STATUS_WORDS = {
    0: "not-determined",
    1: "restricted",
    2: "denied",
    3: "granted",
    4: "write-only",
}
STATES = frozenset(STATUS_WORDS.values())


def grant_instructions(status: str) -> str:
    """How the owner fixes a missing grant. The probe only prompts while undecided;
    after a denial macOS never asks again, so only System Settings helps."""
    if status == "not-installed":
        return f"Install the wrapper first: ./run mcp-app-install, then run: {GRANT_HINT}"
    if status in ("denied", "restricted", "write-only"):
        return f"On the Mac, {SETTINGS_HINT}."
    return f"While the Mac is unlocked, run: {GRANT_HINT}"


def wrapper_reminders_status(app: Path = WRAPPER_APP, timeout: float = 20.0) -> str:
    """The wrapper's Reminders status word, ``not-installed``, or ``unknown (<reason>)``."""
    if demo_mode.is_demo():
        return "unknown (demo mode)"
    app = Path(app)
    if not app.exists():
        return "not-installed"
    with tempfile.TemporaryDirectory() as scratch:
        out = Path(scratch) / "status.txt"
        try:
            proc = subprocess.run(
                [
                    "open",
                    "-W",
                    "-n",
                    "--stdout",
                    str(out),
                    "--stderr",
                    "/dev/null",
                    str(app),
                    "--args",
                    "--reminders-status",
                ],
                capture_output=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return f"unknown (no answer within {timeout:.0f}s)"
        except OSError as e:
            return f"unknown ({type(e).__name__})"
        word = out.read_text().strip() if out.exists() else ""
    if word in STATES:
        return word
    if proc.returncode != 0:
        err = (proc.stderr or b"").decode(errors="replace").strip().splitlines()
        detail = f": {err[0][:160]}" if err else ""
        return f"unknown (open exited {proc.returncode}{detail})"
    return "unknown (the installed app has no --reminders-status; reinstall it with ./run mcp-app-install)"
