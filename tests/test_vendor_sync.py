"""The vendored log-collector client must import and expose what unified_log uses."""

from pathlib import Path

VENDORED = (
    Path(__file__).parent.parent / "src" / "apple_notes" / "_vendor" / "logger.py"
)


def test_vendored_client_imports():
    from apple_notes._vendor import logger as client

    assert callable(client.log)
    assert client.MAX_MESSAGE_LEN == 8192


def test_vendored_client_has_symbols_unified_log_relies_on():
    """unified_log._enqueue reaches into these private client symbols; a
    re-vendored client that renames them must fail loudly here, not
    silently drop every enqueue-only event."""
    from apple_notes._vendor import logger as client

    for name in ("log", "_build_event", "_queue_file", "_append_queue"):
        assert callable(getattr(client, name)), name
