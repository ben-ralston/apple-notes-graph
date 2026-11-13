"""iCloud sync nudge after write/delete operations."""

import logging
import os
import subprocess

from . import demo_mode

logger = logging.getLogger(__name__)

_TRUTHY_VALUES = {"1", "true", "yes"}


def nudge_icloud_sync() -> bool:
    """Nudge iCloud to sync by restarting the bird daemon.

    Controlled by NOTES_GRAPH_SYNC_NUDGE env var ("1", "true", "yes" to enable).
    Never raises — logs warnings on failure.

    Returns:
        True if nudge was attempted, False if disabled.
    """
    value = os.environ.get("NOTES_GRAPH_SYNC_NUDGE", "").strip().lower()
    if value not in _TRUTHY_VALUES or demo_mode.is_demo():
        return False

    try:
        result = subprocess.run(["killall", "bird"], capture_output=True, timeout=5)
        if result.returncode != 0:
            logger.warning(
                "iCloud sync nudge: bird not running or killall failed (rc=%d)",
                result.returncode,
            )
            return False
        logger.info("iCloud sync nudge: restarted bird daemon")
        return True
    except Exception as e:
        logger.warning("iCloud sync nudge failed: %s", e)
        return False
