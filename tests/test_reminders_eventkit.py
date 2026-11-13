"""Real EventKit read against the owner's Reminders (issue #171). Read-only.

Needs a process with Reminders access (the owner's terminal), so it is macos_gui and
never runs in CI. Prints nothing: reminder titles are private.
"""

import pytest

from apple_notes_mcp.reminders.store import EventKitStore

pytestmark = pytest.mark.macos_gui


def test_real_store_reads_lists_and_reminders():
    store = EventKitStore()
    if store.authorization_status() != "granted":
        pytest.skip("this process has no Reminders access")
    assert len(store.lists()) > 0
    items = store.reminders(None, include_completed=False)
    assert all(r.id and r.list_id for r in items)
    assert all(r.completed is False for r in items)
