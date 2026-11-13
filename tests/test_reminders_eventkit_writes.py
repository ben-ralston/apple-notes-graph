"""Real EventKit writes through the Reminders tools (issue #171, Phase 2).

Runs only in the scratch "MCP Test" list, which must already exist (it is never
created here). Every reminder a test makes has a per-run tag in its title, and
the fixture removes every tagged reminder in "MCP Test" afterwards, completed
copies included. the owner's real lists are never written. Needs a process with
Reminders access (the owner's terminal), so it is macos_gui and never runs in CI.
Prints nothing: reminder titles are private.
"""

import datetime as dt
import threading
import uuid

import EventKit
import pytest

from apple_notes_mcp.reminders.server import build_reminders_mcp
from apple_notes_mcp.reminders.store import EventKitStore, due_components, format_due
from reminders_fakes import call
from mcp.server.fastmcp.exceptions import ToolError

pytestmark = pytest.mark.macos_gui

TEST_LIST = "MCP Test"
GUARDED = ("Grocery List", "errands this week!")


def _fetch_all(ek, calendar):
    done, box = threading.Event(), {}

    def on_fetch(items):
        box["items"] = list(items or [])
        done.set()

    ek.fetchRemindersMatchingPredicate_completion_(
        ek.predicateForRemindersInCalendars_([calendar]), on_fetch
    )
    assert done.wait(20), "EventKit fetch timed out"
    return box["items"]


@pytest.fixture
def live():
    store = EventKitStore()
    if store.authorization_status() != "granted":
        pytest.skip("this process has no Reminders access")
    matches = [lst for lst in store.lists() if lst.name == TEST_LIST]
    if len(matches) != 1:
        pytest.skip(f'needs exactly one "{TEST_LIST}" list')
    tag = f"mcp-test-{uuid.uuid4().hex[:8]}"
    # A separate EKEventStore for setup and cleanup, so the tools' store sees
    # these changes the way it would see the owner's phone.
    ek = EventKit.EKEventStore.alloc().init()
    calendar = ek.calendarWithIdentifier_(matches[0].id)
    try:
        yield store, matches[0].id, tag, ek, calendar
    finally:
        for item in _fetch_all(ek, calendar):
            if str(item.title() or "").startswith(tag):
                ok, err = ek.removeReminder_commit_error_(item, True, None)
                assert ok, err
        left = [
            i for i in _fetch_all(ek, calendar) if str(i.title() or "").startswith(tag)
        ]
        assert left == []


def test_live_create_then_complete_non_recurring(live):
    store, list_id, tag, _ek, _cal = live
    mcp = build_reminders_mcp(store, never_complete_lists=GUARDED)
    today = dt.date.today().isoformat()
    made = call(
        mcp,
        "reminders_create",
        {"title": f"{tag} one-off", "list": list_id, "due": today},
    )
    assert made["list_id"] == list_id and made["due"] == today

    with pytest.raises(ToolError, match="expected_due"):
        call(
            mcp, "reminders_complete", {"id": made["id"], "expected_due": "1999-01-01"}
        )
    still = call(mcp, "reminders_list", {"list_name": list_id})["reminders"]
    assert [r["id"] for r in still] == [made["id"]]

    out = call(mcp, "reminders_complete", {"id": made["id"], "expected_due": today})
    assert out["completed_due"] == today and out["next_due"] is None
    assert call(mcp, "reminders_list", {"list_name": list_id})["count"] == 0


def _make_daily(ek, calendar, title, due: dt.date):
    item = EventKit.EKReminder.reminderWithEventStore_(ek)
    item.setTitle_(title)
    item.setCalendar_(calendar)
    item.setDueDateComponents_(due_components(due.isoformat()))
    rule = EventKit.EKRecurrenceRule.alloc().initRecurrenceWithFrequency_interval_end_(
        EventKit.EKRecurrenceFrequencyDaily, 1, None
    )
    item.addRecurrenceRule_(rule)
    ok, err = ek.saveReminder_commit_error_(item, True, None)
    assert ok, err
    return str(item.calendarItemIdentifier())


def test_live_recurring_overdue_daily_completes_once(live):
    store, list_id, tag, ek, calendar = live
    mcp = build_reminders_mcp(store, never_complete_lists=GUARDED)
    today = dt.date.today()
    yesterday = (today - dt.timedelta(days=1)).isoformat()
    # A date-only due date counts as midnight, and EventKit moves a completed
    # recurring reminder to its first occurrence after now: tomorrow, not today.
    tomorrow = (today + dt.timedelta(days=1)).isoformat()
    rid = _make_daily(ek, calendar, f"{tag} daily", today - dt.timedelta(days=1))

    out = call(mcp, "reminders_complete", {"id": rid, "expected_due": yesterday})
    assert out == {
        "id": rid,
        "list": TEST_LIST,
        "recurring": True,
        "completed_due": yesterday,
        "next_due": tomorrow,
    }
    # Same pass, stale expected_due: the compare-and-swap refuses.
    with pytest.raises(ToolError, match="expected_due"):
        call(mcp, "reminders_complete", {"id": rid, "expected_due": yesterday})

    # A later pass reads the new due date; the completed copy from today stops it.
    with pytest.raises(ToolError, match="already completed today"):
        call(mcp, "reminders_complete", {"id": rid, "expected_due": tomorrow})

    open_now = call(mcp, "reminders_list", {"list_name": list_id})["reminders"]
    assert [(r["id"], r["due"], r["completed"]) for r in open_now] == [
        (rid, tomorrow, False)
    ]


def test_live_never_complete_list_refused_without_touching_it(live):
    store, list_id, tag, _ek, _cal = live
    mcp = build_reminders_mcp(store, never_complete_lists=(TEST_LIST.lower(),))
    made = call(mcp, "reminders_create", {"title": f"{tag} guarded", "list": list_id})
    with pytest.raises(ToolError, match="reminders.never_complete_lists"):
        call(mcp, "reminders_complete", {"id": made["id"], "expected_due": None})
    still = call(mcp, "reminders_list", {"list_name": list_id})["reminders"]
    assert [(r["id"], r["completed"]) for r in still] == [(made["id"], False)]


def test_live_completion_from_another_store_is_seen(live):
    """the owner ticks it on his phone after the server's store already loaded it."""
    store, list_id, tag, ek, calendar = live
    mcp = build_reminders_mcp(store, never_complete_lists=GUARDED)
    today = dt.date.today()
    yesterday = (today - dt.timedelta(days=1)).isoformat()
    tomorrow = (today + dt.timedelta(days=1)).isoformat()
    rid = _make_daily(ek, calendar, f"{tag} phone", today - dt.timedelta(days=1))

    # The server's store loads it (a read, then a refused completion by id).
    listed = call(mcp, "reminders_list", {"list_name": list_id})["reminders"]
    assert [(r["id"], r["due"]) for r in listed] == [(rid, yesterday)]
    with pytest.raises(ToolError, match="expected_due"):
        call(mcp, "reminders_complete", {"id": rid, "expected_due": "1999-01-01"})

    # "The phone" completes it through another store.
    item = ek.calendarItemWithIdentifier_(rid)
    item.setCompleted_(True)
    ok, err = ek.saveReminder_commit_error_(item, True, None)
    assert ok, err

    # A stale view would accept the old due date and skip another occurrence.
    with pytest.raises(ToolError, match=f"current due '{tomorrow}'"):
        call(mcp, "reminders_complete", {"id": rid, "expected_due": yesterday})
    with pytest.raises(ToolError, match="already completed today"):
        call(mcp, "reminders_complete", {"id": rid, "expected_due": tomorrow})


def test_live_update_redates_renames_and_clears(live):
    """#232: every edit goes through EventKit and reads back from another store."""
    store, list_id, tag, ek, _cal = live
    mcp = build_reminders_mcp(store, never_complete_lists=GUARDED)
    today = dt.date.today()
    later = (today + dt.timedelta(days=3)).isoformat()
    made = call(
        mcp, "reminders_create", {"title": f"{tag} edit me", "list": list_id, "due": today.isoformat()}
    )
    rid = made["id"]

    with pytest.raises(ToolError, match="expected_due"):
        call(mcp, "reminders_update", {"id": rid, "expected_due": later, "due": later})

    out = call(
        mcp,
        "reminders_update",
        {"id": rid, "expected_due": today.isoformat(), "due": f"{later}T08:30",
         "title": f"{tag} edited", "notes": "n1", "priority": "high"},
    )
    assert out["changed"] == {
        "title": {"before": f"{tag} edit me", "after": f"{tag} edited"},
        "due": {"before": today.isoformat(), "after": f"{later}T08:30"},
        "notes": {"before": None, "after": "n1"},
        "priority": {"before": "none", "after": "high"},
    }
    fresh = EventKit.EKEventStore.alloc().init()
    seen = fresh.calendarItemWithIdentifier_(rid)
    assert str(seen.title()) == f"{tag} edited" and str(seen.notes()) == "n1"
    assert seen.priority() == 1

    out = call(
        mcp,
        "reminders_update",
        {"id": rid, "expected_due": f"{later}T08:30", "clear_due": True, "clear_notes": True},
    )
    assert out["changed"] == {
        "due": {"before": f"{later}T08:30", "after": None},
        "notes": {"before": "n1", "after": None},
    }
    listed = call(mcp, "reminders_list", {"list_name": list_id, "include_notes": True})
    assert [(r["id"], r["due"], r["notes"]) for r in listed["reminders"]] == [(rid, None, None)]

    call(mcp, "reminders_create", {"title": f"{tag} other", "list": list_id})
    with pytest.raises(ToolError, match="same title"):
        call(mcp, "reminders_update", {"id": rid, "expected_due": None, "title": f"{tag} OTHER"})


def test_live_update_refuses_recurring_due_but_renames_it(live):
    store, list_id, tag, ek, calendar = live
    mcp = build_reminders_mcp(store, never_complete_lists=GUARDED)
    today = dt.date.today()
    rid = _make_daily(ek, calendar, f"{tag} daily", today)
    with pytest.raises(ToolError, match="whole series"):
        call(mcp, "reminders_update", {"id": rid, "expected_due": today.isoformat(),
                                       "due": (today + dt.timedelta(days=2)).isoformat()})
    out = call(mcp, "reminders_update", {"id": rid, "expected_due": today.isoformat(),
                                         "title": f"{tag} daily (am)"})
    assert out["changed"] == {"title": {"before": f"{tag} daily", "after": f"{tag} daily (am)"}}
    listed = call(mcp, "reminders_list", {"list_name": list_id})["reminders"]
    assert [(r["id"], r["due"], r["recurring"]) for r in listed] == [(rid, today.isoformat(), True)]


def _alarmed(ek, calendar, title, due, alarm_times, start=False):
    item = EventKit.EKReminder.reminderWithEventStore_(ek)
    item.setTitle_(title)
    item.setCalendar_(calendar)
    item.setDueDateComponents_(due_components(due))
    if start:
        item.setStartDateComponents_(due_components(due))
    for at in alarm_times:
        item.addAlarm_(EventKit.EKAlarm.alarmWithAbsoluteDate_(
            EventKit.NSDate.dateWithTimeIntervalSince1970_(dt.datetime.fromisoformat(at).timestamp())))
    ok, err = ek.saveReminder_commit_error_(item, True, None)
    assert ok, err
    return str(item.calendarItemIdentifier())


def _read_back(calendar, rid):
    """The reminder as a separate EKEventStore sees it: (due, start, alarm times)."""
    fresh = EventKit.EKEventStore.alloc().init()
    item = next(
        i for i in _fetch_all(fresh, fresh.calendarWithIdentifier_(calendar.calendarIdentifier()))
        if str(i.calendarItemIdentifier()) == rid
    )
    start = item.startDateComponents()
    times = sorted(
        dt.datetime.fromtimestamp(a.absoluteDate().timeIntervalSince1970()).strftime("%Y-%m-%dT%H:%M")
        for a in (item.alarms() or [])
    )
    return format_due(item.dueDateComponents()), (format_due(start) if start else None), times


def test_live_update_moves_the_alert_with_the_due_date(live):
    """the owner's review of #232: a re-date moves the alert at the due time (and the
    start date Reminders.app keeps equal to it); clearing the due date drops it."""
    store, list_id, tag, ek, calendar = live
    mcp = build_reminders_mcp(store, never_complete_lists=GUARDED)
    day = dt.date.today() + dt.timedelta(days=2)
    old, new = f"{day.isoformat()}T09:00", f"{(day + dt.timedelta(days=3)).isoformat()}T10:30"
    rid = _alarmed(ek, calendar, f"{tag} alerted", old, [old], start=True)

    out = call(mcp, "reminders_update", {"id": rid, "expected_due": old, "due": new})
    assert out["changed"] == {
        "due": {"before": old, "after": new},
        "alerts": {"before": [old], "after": [new]},
    }
    assert _read_back(calendar, rid) == (new, new, [new])

    out = call(mcp, "reminders_update", {"id": rid, "expected_due": new, "clear_due": True})
    assert out["changed"]["alerts"] == {"before": [new], "after": []}
    assert _read_back(calendar, rid) == (None, None, [])


def test_live_update_keeps_a_day_before_alert_a_day_before(live):
    store, list_id, tag, ek, calendar = live
    mcp = build_reminders_mcp(store, never_complete_lists=GUARDED)
    day = dt.date.today() + dt.timedelta(days=2)
    old = f"{day.isoformat()}T09:00"
    before = f"{(day - dt.timedelta(days=1)).isoformat()}T09:00"
    new = f"{(day + dt.timedelta(days=4)).isoformat()}T09:00"
    new_before = f"{(day + dt.timedelta(days=3)).isoformat()}T09:00"
    rid = _alarmed(ek, calendar, f"{tag} two alerts", old, [old, before])

    call(mcp, "reminders_update", {"id": rid, "expected_due": old, "due": new})
    due, _start, alarms = _read_back(calendar, rid)  # EventKit may set start = due itself
    assert (due, alarms) == (new, sorted([new, new_before]))
    with pytest.raises(ToolError, match="another alert"):
        call(mcp, "reminders_update", {"id": rid, "expected_due": new, "clear_due": True})
    assert _read_back(calendar, rid)[0] == new
