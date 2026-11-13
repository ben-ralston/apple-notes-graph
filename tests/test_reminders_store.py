"""EventKit reminders store: field mapping, access and timeout handling (#171).

No TCC needed: EKReminder objects are stubbed and dates are plain
NSDateComponents, so this module runs in CI.
"""

from pathlib import Path

import pytest
from Foundation import NSDate, NSDateComponents

from apple_notes_mcp.reminders import store as rs


def _comps(y, m, d, hour=None, minute=None):
    c = NSDateComponents.alloc().init()
    c.setYear_(y)
    c.setMonth_(m)
    c.setDay_(d)
    if hour is not None:
        c.setHour_(hour)
        c.setMinute_(minute or 0)
    return c


class StubCalendar:
    def __init__(self, cal_id="cal-1", title="Reminders", source="iCloud"):
        self._id, self._title, self._source = cal_id, title, source

    def calendarIdentifier(self):
        return self._id

    def title(self):
        return self._title

    def source(self):
        source = self._source

        class _Source:
            def title(self):
                return source

            def sourceIdentifier(self):
                return f"src-{source}"

        return _Source()


class StubEK:
    def __init__(
        self,
        title="T",
        due=None,
        recurring=False,
        completed=False,
        completion=None,
        notes=None,
        calendar=None,
        item_id="rem-1",
        priority=0,
    ):
        self._title, self._due, self._recurring = title, due, recurring
        self._priority = priority
        self._completed, self._completion, self._notes = completed, completion, notes
        self._calendar, self._id = calendar or StubCalendar(), item_id

    def calendarItemIdentifier(self):
        return self._id

    def title(self):
        return self._title

    def calendar(self):
        return self._calendar

    def dueDateComponents(self):
        return self._due

    def hasRecurrenceRules(self):
        return self._recurring

    def isCompleted(self):
        return self._completed

    def completionDate(self):
        return self._completion

    def notes(self):
        return self._notes

    def priority(self):
        return self._priority


class NeverCallsBackStore:
    """An event store whose reminder fetch never completes (an iCloud stall)."""

    def calendarsForEntityType_(self, _entity):
        return [StubCalendar()]

    def refreshSourcesIfNecessary(self):
        pass

    def predicateForIncompleteRemindersWithDueDateStarting_ending_calendars_(self, *_):
        return object()

    def predicateForRemindersInCalendars_(self, _cals):
        return object()

    def fetchRemindersMatchingPredicate_completion_(self, _pred, _callback):
        return None


def test_format_due_date_only_and_with_time():
    assert rs.format_due(_comps(2026, 10, 27)) == "2026-10-27"
    assert rs.format_due(_comps(2026, 10, 27, 9, 5)) == "2026-10-27T09:05"
    assert rs.format_due(None) is None


def test_format_completed_at_is_local_minutes():
    ts = 1_800_000_000
    expected = (
        __import__("datetime").datetime.fromtimestamp(ts).strftime("%Y-%m-%dT%H:%M")
    )
    assert rs.format_completed_at(NSDate.dateWithTimeIntervalSince1970_(ts)) == expected
    assert rs.format_completed_at(None) is None


def test_reminder_from_ek_maps_every_field():
    r = rs.reminder_from_ek(
        StubEK(title="T", due=_comps(2026, 10, 24), recurring=True, notes="n")
    )
    assert (r.id, r.title, r.list_id, r.list) == ("rem-1", "T", "cal-1", "Reminders")
    assert (r.due, r.recurring, r.completed, r.completed_at, r.notes) == (
        "2026-10-24",
        True,
        False,
        None,
        "n",
    )


def test_status_mapping():
    for raw, word in [
        (0, "not-determined"),
        (1, "restricted"),
        (2, "denied"),
        (3, "granted"),
        (4, "write-only"),
    ]:
        store = rs.EventKitStore(event_store=object(), status_fn=lambda raw=raw: raw)
        assert store.authorization_status() == word


def test_reads_refused_without_grant():
    s = rs.EventKitStore(event_store=object(), status_fn=lambda: 2)
    with pytest.raises(rs.RemindersAccessError) as e:
        s.lists()
    assert e.value.status == "denied"
    with pytest.raises(rs.RemindersAccessError):
        s.reminders(None, include_completed=False)


def test_lists_maps_calendars():
    class Store(NeverCallsBackStore):
        def calendarsForEntityType_(self, _entity):
            return [
                StubCalendar("a", "Reminders", "iCloud"),
                StubCalendar("b", "Reminders", "Exchange"),
            ]

    lists = rs.EventKitStore(event_store=Store(), status_fn=lambda: 3).lists()
    assert lists == [
        rs.ReminderList(id="a", name="Reminders", source="iCloud"),
        rs.ReminderList(id="b", name="Reminders", source="Exchange"),
    ]


def test_reminders_fetch_returns_mapped_items():
    class Store(NeverCallsBackStore):
        def fetchRemindersMatchingPredicate_completion_(self, _pred, callback):
            callback([StubEK(title="x"), StubEK(title="y", item_id="rem-2")])

    items = rs.EventKitStore(event_store=Store(), status_fn=lambda: 3).reminders(
        None, include_completed=False
    )
    assert [r.title for r in items] == ["x", "y"]


def test_fetch_that_never_completes_times_out():
    s = rs.EventKitStore(
        event_store=NeverCallsBackStore(), status_fn=lambda: 3, fetch_timeout=0.2
    )
    with pytest.raises(rs.RemindersTimeoutError):
        s.reminders(None, include_completed=False)


def test_store_module_only_completes_creates_and_edits():
    """Writes complete, create and edit (#171, #232): never delete, uncomplete,
    touch lists, recurrence or alarms."""
    import re

    src = Path(rs.__file__).read_text()
    for forbidden in (
        "removeReminder",
        "saveCalendar",
        "removeCalendar",
        "requestAccess",
        "requestFullAccess",
        "addRecurrenceRule",
        "setRecurrenceRules",
        "addAlarm",
        "removeAlarm",
    ):
        assert forbidden not in src
    # Alerts change only by moving with the due date (#232).
    assert src.count("setAlarms_(") == 1
    assert re.findall(r"setCompleted_\((.*?)\)", src) == ["True"]


def test_unknown_list_ids_return_nothing_not_everything():
    class Store(NeverCallsBackStore):
        def fetchRemindersMatchingPredicate_completion_(self, _pred, callback):
            callback([StubEK(title="from every list")])

    items = rs.EventKitStore(event_store=Store(), status_fn=lambda: 3).reminders(
        ["no-such-id"], include_completed=False
    )
    assert items == []


# --- multi-review round 1 (#171) -------------------------------------------

import datetime as _dt
import threading as _threading
import time as _time


class RecordingStore(NeverCallsBackStore):
    """Answers fetches from a dict keyed by predicate name; records calls."""

    def __init__(self, answers=None, on_thread=False, delay=0.0):
        self.answers = answers or {}
        self.calls = []
        self.cancelled = []
        self.on_thread = on_thread
        self.delay = delay
        self.active = 0
        self.max_active = 0

    def predicateForIncompleteRemindersWithDueDateStarting_ending_calendars_(self, *args):
        self.calls.append(("incomplete", args))
        return "incomplete"

    def predicateForCompletedRemindersWithCompletionDateStarting_ending_calendars_(self, *args):
        self.calls.append(("completed", args))
        return "completed"

    def predicateForRemindersInCalendars_(self, cals):
        self.calls.append(("all", (cals,)))
        return "all"

    def fetchRemindersMatchingPredicate_completion_(self, pred, callback):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        _time.sleep(self.delay)
        self.active -= 1
        items = self.answers.get(pred, [])
        if self.on_thread:
            _threading.Thread(target=callback, args=(items,)).start()
        else:
            callback(items)
        return f"req-{pred}"

    def cancelFetchRequest_(self, request):
        self.cancelled.append(request)


def test_reads_run_inside_an_autorelease_pool(monkeypatch):
    import contextlib

    entered = []

    @contextlib.contextmanager
    def pool():
        entered.append(1)
        yield

    monkeypatch.setattr(rs.objc, "autorelease_pool", pool)
    s = rs.EventKitStore(event_store=RecordingStore(), status_fn=lambda: 3)
    s.lists()
    s.reminders(None, include_completed=False)
    assert len(entered) >= 2


def test_reminders_are_converted_on_the_callback_thread():
    seen = []

    class ThreadStub(StubEK):
        def title(self):
            seen.append(_threading.current_thread().name)
            return super().title()

    store = RecordingStore({"incomplete": [ThreadStub()]}, on_thread=True)
    items = rs.EventKitStore(event_store=store, status_fn=lambda: 3).reminders(
        None, include_completed=False
    )
    assert [r.title for r in items] == ["T"]
    assert seen and seen[0] != _threading.current_thread().name


def test_concurrent_reads_are_serialized():
    store = RecordingStore({"incomplete": [StubEK()]}, delay=0.05)
    s = rs.EventKitStore(event_store=store, status_fn=lambda: 3)
    threads = [
        _threading.Thread(target=s.reminders, args=(None, False)) for _ in range(3)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert store.max_active == 1


def test_waiting_for_a_stalled_read_is_bounded():
    """A read stuck where EventKit has no timeout (refresh) must not queue the rest forever."""
    release = _threading.Event()

    class HangsOnRefresh(NeverCallsBackStore):
        def refreshSourcesIfNecessary(self):
            release.wait(5)

    s = rs.EventKitStore(event_store=HangsOnRefresh(), status_fn=lambda: 3, fetch_timeout=0.3)
    first = _threading.Thread(target=s.lists)
    first.start()
    _time.sleep(0.05)
    started = _time.monotonic()
    try:
        with pytest.raises(rs.RemindersTimeoutError, match="busy"):
            s.lists()
        assert _time.monotonic() - started < 1.0
    finally:
        release.set()
        first.join()


def test_store_is_rebuilt_after_access_is_revoked():
    statuses = iter([3, 2, 3])
    built = []

    def factory():
        built.append(1)
        return RecordingStore()

    s = rs.EventKitStore(status_fn=lambda: next(statuses), store_factory=factory)
    s.lists()
    with pytest.raises(rs.RemindersAccessError):
        s.lists()
    s.lists()
    assert len(built) == 2


def test_timed_out_fetch_is_cancelled():
    class Stalls(RecordingStore):
        def fetchRemindersMatchingPredicate_completion_(self, pred, callback):
            return "req-stalled"

    store = Stalls()
    s = rs.EventKitStore(event_store=store, status_fn=lambda: 3, fetch_timeout=0.1)
    with pytest.raises(rs.RemindersTimeoutError):
        s.reminders(None, include_completed=False)
    assert store.cancelled == ["req-stalled"]


def test_completed_reminders_are_bounded_by_since():
    store = RecordingStore(
        {"incomplete": [StubEK(title="open")], "completed": [StubEK(title="done", completed=True)]}
    )
    since = _dt.datetime(2026, 9, 27)
    items = rs.EventKitStore(event_store=store, status_fn=lambda: 3).reminders(
        None, include_completed=True, completed_since=since
    )
    assert sorted(r.title for r in items) == ["done", "open"]
    kinds = [kind for kind, _ in store.calls]
    assert "all" not in kinds and "completed" in kinds
    start = next(args[0] for kind, args in store.calls if kind == "completed")
    assert abs(start.timeIntervalSince1970() - since.timestamp()) < 1


def test_status_words_live_in_grant_only():
    from apple_notes_mcp.reminders import grant

    assert grant.STATES == frozenset(grant.STATUS_WORDS.values())
    assert not hasattr(rs, "GRANT_HINT")
    assert not hasattr(rs, "_STATUS_WORDS")


# --- Phase 2: completing and creating (#171) --------------------------------

from apple_notes_mcp.reminders.guards import RemindersRefusal

TODAY = _dt.date(2026, 10, 27)
GUARDED = ("Grocery List", "errands this week!")


def _at(*args):
    return NSDate.dateWithTimeIntervalSince1970_(_dt.datetime(*args).timestamp())


class WritableCalendar(StubCalendar):
    def __init__(self, *a, writable=True, **kw):
        super().__init__(*a, **kw)
        self._writable = writable

    def allowsContentModifications(self):
        return self._writable


class WritableEK(StubEK):
    """An EKReminder stand-in that records mutations."""

    def __init__(self, *a, is_reminder=True, **kw):
        super().__init__(*a, **kw)
        self._is_reminder = is_reminder
        self.rolled_back = False

    def isKindOfClass_(self, _cls):
        return self._is_reminder

    def setCompleted_(self, value):
        self._completed = value

    def rollback(self):
        self.rolled_back = True
        self._completed = False

    def setTitle_(self, value):
        self._title = value

    def setCalendar_(self, value):
        self._calendar = value

    def setNotes_(self, value):
        self._notes = value

    def setDueDateComponents_(self, value):
        self._due = value

    def setPriority_(self, value):
        self._priority = value

    def alarms(self):
        return []

    def startDateComponents(self):
        return None


class StubNSError:
    def domain(self):
        return "EKErrorDomain"

    def code(self):
        return 17


class WritableStore(RecordingStore):
    """Saves like EventKit: completing a recurring reminder leaves it open with
    its due date moved to ``next_due`` and adds a completed copy."""

    def __init__(self, items=(), calendars=(), next_due=None, fail=False, raises=None, **kw):
        super().__init__(**kw)
        self.items = {i.calendarItemIdentifier(): i for i in items}
        self.cals = {c.calendarIdentifier(): c for c in calendars}
        self.next_due, self.fail, self.raises = next_due, fail, raises
        self.saved = []
        self.resets = 0

    def reset(self):
        self.resets += 1

    def calendarsForEntityType_(self, _entity):
        return list(self.cals.values())

    def calendarItemWithIdentifier_(self, item_id):
        return self.items.get(item_id)

    def calendarWithIdentifier_(self, cal_id):
        return self.cals.get(cal_id)

    def saveReminder_commit_error_(self, item, commit, _err):
        assert commit is True
        if self.raises is not None:
            raise self.raises
        if self.fail:
            return (False, StubNSError())
        self.saved.append(item)
        self.items[item.calendarItemIdentifier()] = item
        if item.hasRecurrenceRules() and item.isCompleted():
            item._completed = False
            item._due = self.next_due
        return (True, None)


def _writable(store):
    return rs.EventKitStore(
        event_store=store,
        status_fn=lambda: 3,
        today_fn=lambda: TODAY,
        new_reminder=lambda _s: WritableEK(item_id="new-1", title=""),
    )


CAL = WritableCalendar("cal-1", "Reminders")


def test_complete_saves_and_reports_next_due_for_recurring():
    item = WritableEK(
        title="Pay rent", due=_comps(2026, 10, 24), recurring=True, calendar=CAL, item_id="r1"
    )
    store = WritableStore([item], [CAL], next_due=_comps(2026, 11, 24))
    done = _writable(store).complete("r1", "2026-10-24", GUARDED)
    assert done == rs.Completion(
        id="r1", list="Reminders", recurring=True,
        completed_due="2026-10-24", next_due="2026-11-24",
    )
    assert store.saved == [item]


def test_complete_non_recurring_has_no_next_due():
    item = WritableEK(due=_comps(2026, 10, 27, 9, 0), calendar=CAL, item_id="r2")
    store = WritableStore([item], [CAL])
    done = _writable(store).complete("r2", "2026-10-27T09:00", GUARDED)
    assert done.completed_due == "2026-10-27T09:00"
    assert done.next_due is None and done.recurring is False
    assert item.isCompleted() is True


def test_stale_expected_due_is_refused_and_nothing_saved():
    item = WritableEK(due=_comps(2026, 10, 27), recurring=True, calendar=CAL, item_id="r1")
    store = WritableStore([item], [CAL])
    with pytest.raises(RemindersRefusal, match="expected_due '2026-10-24'"):
        _writable(store).complete("r1", "2026-10-24", GUARDED)
    assert store.saved == [] and item.isCompleted() is False


def test_blocklisted_list_refused_before_any_write():
    grocery = WritableCalendar("g", "Grocery List")
    item = WritableEK(due=None, calendar=grocery, item_id="r4")
    store = WritableStore([item], [grocery])
    with pytest.raises(RemindersRefusal, match="never_complete_lists"):
        _writable(store).complete("r4", None, GUARDED)
    assert store.saved == []


def test_completed_today_lookup_only_for_recurring():
    copy = WritableEK(
        title="Vitamins", completed=True, completion=_at(2026, 10, 27, 9, 0),
        calendar=CAL, item_id="copy-1",
    )
    plain = WritableEK(title="Vitamins", due=_comps(2026, 10, 28), calendar=CAL, item_id="p1")
    store = WritableStore([plain], [CAL], answers={"completed": [copy]})
    _writable(store).complete("p1", "2026-10-28", GUARDED)
    assert "completed" not in [kind for kind, _ in store.calls]

    daily = WritableEK(
        title="Vitamins", due=_comps(2026, 10, 28), recurring=True, calendar=CAL, item_id="d1"
    )
    store = WritableStore([daily], [CAL], answers={"completed": [copy]})
    with pytest.raises(RemindersRefusal, match="already completed today"):
        _writable(store).complete("d1", "2026-10-28", GUARDED)
    assert store.saved == []
    start, end, cals = next(args for kind, args in store.calls if kind == "completed")
    midnight = _dt.datetime.combine(TODAY, _dt.time()).timestamp()
    assert abs(start.timeIntervalSince1970() - midnight) < 1
    assert end is None and [c.calendarIdentifier() for c in cals] == ["cal-1"]


def test_unknown_id_and_non_reminder_item_are_not_found():
    event = WritableEK(calendar=CAL, item_id="ev1", is_reminder=False)
    s = _writable(WritableStore([event], [CAL]))
    with pytest.raises(rs.RemindersNotFoundError):
        s.complete("nope", None, GUARDED)
    with pytest.raises(rs.RemindersNotFoundError):
        s.complete("ev1", None, GUARDED)
    with pytest.raises(rs.RemindersNotFoundError):
        s.create("Title", "no-such-list", None, None)


def test_read_only_list_is_refused_for_complete_and_create():
    shared = WritableCalendar("s1", "BJ's Reminders", writable=False)
    item = WritableEK(due=None, calendar=shared, item_id="r5")
    store = WritableStore([item], [shared])
    s = _writable(store)
    with pytest.raises(RemindersRefusal, match="BJ's Reminders\" is read-only in Reminders; nothing was completed"):
        s.complete("r5", None, GUARDED)
    with pytest.raises(RemindersRefusal, match="read-only in Reminders; nothing was created"):
        s.create("Title", "s1", None, None)
    assert store.saved == []


def test_failed_save_rolls_back_and_raises_write_error():
    item = WritableEK(title="SECRET-TITLE", due=None, calendar=CAL, item_id="r6")
    store = WritableStore([item], [CAL], fail=True)
    with pytest.raises(rs.RemindersWriteError) as e:
        _writable(store).complete("r6", None, GUARDED)
    assert str(e.value) == "save failed: EKErrorDomain 17"
    assert item.rolled_back is True and item.isCompleted() is False

    with pytest.raises(rs.RemindersWriteError) as e:
        _writable(store).create("SECRET-TITLE", "cal-1", None, None)
    assert "SECRET" not in str(e.value)


def test_create_sets_fields_and_returns_reminder():
    store = WritableStore([], [CAL])
    r = _writable(store).create("Buy stamps", "cal-1", "2026-10-30T09:15", "at the PO")
    assert (r.id, r.title, r.list_id, r.list) == ("new-1", "Buy stamps", "cal-1", "Reminders")
    assert r.due == "2026-10-30T09:15" and r.notes == "at the PO"
    assert r.recurring is False and r.completed is False
    assert len(store.saved) == 1


def test_create_without_due_or_notes_leaves_them_unset():
    store = WritableStore([], [CAL])
    r = _writable(store).create("Buy stamps", "cal-1", None, None)
    assert r.due is None and r.notes is None


def test_create_refuses_open_duplicate_before_saving():
    existing = WritableEK(title="Buy Stamps", calendar=CAL, item_id="r7")
    store = WritableStore([existing], [CAL], answers={"incomplete": [existing]})
    with pytest.raises(RemindersRefusal, match=r"same title is already in list \"Reminders\" \(id r7\)"):
        _writable(store).create("buy stamps", "cal-1", None, None)
    assert store.saved == []
    _, _, cals = next(args for kind, args in store.calls if kind == "incomplete")
    assert [c.calendarIdentifier() for c in cals] == ["cal-1"]


@pytest.mark.parametrize("due", ["2026-10-27", "2026-10-27T09:05", "2026-01-02T00:00"])
def test_due_components_round_trip(due):
    assert rs.format_due(rs.due_components(due)) == due


def test_writes_refused_without_grant():
    s = rs.EventKitStore(event_store=WritableStore([], [CAL]), status_fn=lambda: 2)
    with pytest.raises(rs.RemindersAccessError):
        s.complete("r1", None, GUARDED)
    with pytest.raises(rs.RemindersAccessError):
        s.create("Title", "cal-1", None, None)


def test_writes_share_the_read_lock():
    release = _threading.Event()

    class HangsOnRefresh(WritableStore):
        def refreshSourcesIfNecessary(self):
            release.wait(5)

    s = rs.EventKitStore(
        event_store=HangsOnRefresh([], [CAL]), status_fn=lambda: 3, fetch_timeout=0.3
    )
    first = _threading.Thread(target=s.lists)
    first.start()
    _time.sleep(0.05)
    try:
        with pytest.raises(rs.RemindersTimeoutError, match="busy"):
            s.create("Title", "cal-1", None, None)
    finally:
        release.set()
        first.join()


# --- final review fixes (#171) -----------------------------------------------


def test_failed_save_also_resets_the_store():
    """Nothing the failed save touched may stay pending for a later commit."""
    item = WritableEK(due=None, calendar=CAL, item_id="r6")
    store = WritableStore([item], [CAL], fail=True)
    with pytest.raises(rs.RemindersWriteError):
        _writable(store).complete("r6", None, GUARDED)
    assert store.resets == 1


def test_save_that_raises_rolls_back_resets_and_reports():
    item = WritableEK(title="SECRET-TITLE", due=None, calendar=CAL, item_id="r8")
    store = WritableStore([item], [CAL], raises=RuntimeError("SECRET-TITLE exploded"))
    with pytest.raises(rs.RemindersWriteError) as e:
        _writable(store).complete("r8", None, GUARDED)
    assert str(e.value) == "save failed: raised RuntimeError"
    assert e.value.__cause__ is None and e.value.__context__ is None
    assert item.rolled_back is True and item.isCompleted() is False
    assert store.resets == 1 and store.saved == []



def test_cleanup_failure_still_raises_an_unchained_write_error():
    class BadRollback(WritableEK):
        def rollback(self):
            raise RuntimeError("SECRET-TITLE rollback")

    item = BadRollback(title="SECRET-TITLE", due=None, calendar=CAL, item_id="r9")
    store = WritableStore([item], [CAL], raises=RuntimeError("SECRET-TITLE exploded"))
    with pytest.raises(rs.RemindersWriteError) as e:
        _writable(store).complete("r9", None, GUARDED)
    assert str(e.value) == "save failed: raised RuntimeError"
    assert e.value.__cause__ is None and e.value.__context__ is None


def test_guards_1_to_3_run_before_the_read_only_check_and_the_fetch():
    locked = WritableCalendar("g", "Grocery List", writable=False)
    item = WritableEK(due=_comps(2026, 10, 27), recurring=True, calendar=locked, item_id="r10")
    store = WritableStore([item], [locked])
    with pytest.raises(RemindersRefusal, match="never_complete_lists"):
        _writable(store).complete("r10", "2026-10-27", GUARDED)
    stale = WritableEK(due=_comps(2026, 10, 27), recurring=True, calendar=CAL, item_id="r11")
    store = WritableStore([stale], [CAL])
    with pytest.raises(RemindersRefusal, match="expected_due"):
        _writable(store).complete("r11", "2026-10-20", GUARDED)
    assert "completed" not in [kind for kind, _ in store.calls]


def test_complete_reads_today_once():
    calls = []

    def today():
        calls.append(1)
        return TODAY

    item = WritableEK(due=_comps(2026, 10, 24), recurring=True, calendar=CAL, item_id="r1")
    store = WritableStore([item], [CAL], next_due=_comps(2026, 11, 24))
    s = rs.EventKitStore(event_store=store, status_fn=lambda: 3, today_fn=today)
    s.complete("r1", "2026-10-24", GUARDED)
    assert len(calls) == 1


# --- updating (#232) ----------------------------------------------------------

from apple_notes_mcp.reminders.guards import ReminderEdit

WORK = WritableCalendar("cal-w", "Work")


def test_priority_words_map_eventkit_ranges():
    assert [rs.priority_word(p) for p in (0, 1, 4, 5, 6, 9)] == [
        "none", "high", "high", "medium", "low", "low",
    ]
    assert [rs.PRIORITY_VALUES[w] for w in ("none", "high", "medium", "low")] == [0, 1, 5, 9]
    assert rs.reminder_from_ek(StubEK(priority=5)).priority == "medium"


@pytest.mark.parametrize(
    "edit, expected",
    [
        (ReminderEdit(due="2026-11-03"), "2026-11-03"),
        (ReminderEdit(due="2026-11-03T14:30"), "2026-11-03T14:30"),
        (ReminderEdit(clear_due=True), None),
    ],
)
def test_update_redates_and_clears_due(edit, expected):
    item = WritableEK(title="Dentist", due=_comps(2026, 10, 27), calendar=CAL, item_id="r1")
    store = WritableStore([item], [CAL])
    out = _writable(store).update("r1", edit, "2026-10-27", None)
    assert out == rs.Update(
        id="r1",
        list="Reminders",
        list_id="cal-1",
        changes={"due": {"before": "2026-10-27", "after": expected}},
    )
    assert store.saved == [item] and rs.format_due(item.dueDateComponents()) == expected


def test_update_renames_edits_notes_and_priority():
    item = WritableEK(title="Dentist", notes="old", calendar=CAL, item_id="r1")
    store = WritableStore([item], [CAL])
    out = _writable(store).update(
        "r1", ReminderEdit(title="Dentist (moved)", clear_notes=True, priority="high"), None, "Dentist"
    )
    assert out.changes == {
        "title": {"before": "Dentist", "after": "Dentist (moved)"},
        "notes": {"before": "old", "after": None},
        "priority": {"before": "none", "after": "high"},
    }
    assert (item.title(), item.notes(), item.priority()) == ("Dentist (moved)", None, 1)


def test_update_rename_onto_an_open_reminder_is_refused():
    item = WritableEK(title="Dentist", calendar=CAL, item_id="r1")
    other = WritableEK(title="Call mom", calendar=CAL, item_id="r2")
    store = WritableStore([item, other], [CAL], answers={"incomplete": [item, other]})
    with pytest.raises(RemindersRefusal, match=r"same title.*\(id r2\); nothing was changed"):
        _writable(store).update("r1", ReminderEdit(title="call MOM"), None, None)
    assert store.saved == [] and item.title() == "Dentist"
    _, _, cals = next(args for kind, args in store.calls if kind == "incomplete")
    assert [c.calendarIdentifier() for c in cals] == ["cal-1"]


def test_update_moves_to_another_list_and_checks_duplicates_there():
    item = WritableEK(title="Dentist", calendar=CAL, item_id="r1")
    store = WritableStore([item], [CAL, WORK])
    out = _writable(store).update("r1", ReminderEdit(list_id="cal-w"), None, None)
    assert (out.list, out.list_id) == ("Work", "cal-w")
    assert out.changes == {"list": {"before": "Reminders", "after": "Work"}}
    assert item.calendar() is WORK
    _, _, cals = next(args for kind, args in store.calls if kind == "incomplete")
    assert [c.calendarIdentifier() for c in cals] == ["cal-w"]

    clash = WritableEK(title="dentist", calendar=CAL, item_id="r9")
    item2 = WritableEK(title="Dentist", calendar=WORK, item_id="r2")
    store = WritableStore([item2, clash], [CAL, WORK], answers={"incomplete": [clash]})
    with pytest.raises(RemindersRefusal, match=r"\(id r9\); nothing was changed"):
        _writable(store).update("r2", ReminderEdit(list_id="cal-1"), None, None)
    assert store.saved == [] and item2.calendar() is WORK


def test_update_refuses_read_only_source_or_target_and_unknown_target():
    shared = WritableCalendar("s1", "BJ's Reminders", writable=False)
    mine = WritableEK(calendar=CAL, item_id="r1")
    theirs = WritableEK(calendar=shared, item_id="r2")
    store = WritableStore([mine, theirs], [CAL, shared])
    s = _writable(store)
    with pytest.raises(RemindersRefusal, match="BJ's Reminders\" is read-only in Reminders; nothing was changed"):
        s.update("r2", ReminderEdit(title="New"), None, None)
    with pytest.raises(RemindersRefusal, match="BJ's Reminders\" is read-only in Reminders; nothing was changed"):
        s.update("r1", ReminderEdit(list_id="s1"), None, None)
    with pytest.raises(rs.RemindersNotFoundError):
        s.update("r1", ReminderEdit(list_id="gone"), None, None)
    assert store.saved == []


def test_update_stale_expected_due_or_title_changes_nothing():
    item = WritableEK(title="Dentist", due=_comps(2026, 10, 27), calendar=CAL, item_id="r1")
    store = WritableStore([item], [CAL])
    with pytest.raises(RemindersRefusal, match="expected_due"):
        _writable(store).update("r1", ReminderEdit(due="2026-11-01"), "2026-10-20", None)
    with pytest.raises(RemindersRefusal, match="expected_title"):
        _writable(store).update("r1", ReminderEdit(due="2026-11-01"), "2026-10-27", "Doctor")
    assert store.saved == [] and rs.format_due(item.dueDateComponents()) == "2026-10-27"
    assert store.calls == []


def test_update_refuses_completed_recurring_due_and_no_change():
    done = WritableEK(completed=True, calendar=CAL, item_id="r1")
    daily = WritableEK(title="Meds", due=_comps(2026, 10, 27), recurring=True, calendar=CAL, item_id="r2")
    store = WritableStore([done, daily], [CAL])
    s = _writable(store)
    with pytest.raises(RemindersRefusal, match="already completed"):
        s.update("r1", ReminderEdit(title="X"), None, None)
    with pytest.raises(RemindersRefusal, match="repeats"):
        s.update("r2", ReminderEdit(due="2026-10-29"), "2026-10-27", None)
    with pytest.raises(RemindersRefusal, match="changes nothing"):
        s.update("r2", ReminderEdit(title="Meds"), "2026-10-27", None)
    assert store.saved == []
    out = s.update("r2", ReminderEdit(title="Meds (am)"), "2026-10-27", None)
    assert out.changes == {"title": {"before": "Meds", "after": "Meds (am)"}}


def test_update_unknown_id_and_failed_save():
    s = _writable(WritableStore([], [CAL]))
    with pytest.raises(rs.RemindersNotFoundError):
        s.update("nope", ReminderEdit(title="X"), None, None)

    item = WritableEK(title="SECRET-TITLE", calendar=CAL, item_id="r1")
    store = WritableStore([item], [CAL], fail=True)
    with pytest.raises(rs.RemindersWriteError) as e:
        _writable(store).update("r1", ReminderEdit(title="SECRET-NEW"), None, None)
    assert "SECRET" not in str(e.value)
    assert item.rolled_back is True and store.resets == 1


def test_update_refused_without_grant():
    s = rs.EventKitStore(event_store=WritableStore([], [CAL]), status_fn=lambda: 2)
    with pytest.raises(rs.RemindersAccessError):
        s.update("r1", ReminderEdit(title="X"), None, None)


# --- review round 1 (#232) ----------------------------------------------------


class StubAlarm:
    def __init__(self, location=False, at=None, offset=0.0, sound=None):
        self._location, self._offset, self._sound = location, offset, sound
        self._date = _at(*at) if at else None

    def structuredLocation(self):
        return object() if self._location else None

    def absoluteDate(self):
        return self._date

    def soundName(self):
        return self._sound

    def relativeOffset(self):
        return self._offset

    def copy(self):
        twin = StubAlarm(location=self._location, offset=self._offset, sound=self._sound)
        twin._date = self._date
        return twin


class AlarmEK(WritableEK):
    def __init__(self, *a, alarms=(), start=None, **kw):
        super().__init__(*a, **kw)
        self._alarms, self._start = list(alarms), start
        self.set_alarms_calls = 0

    def alarms(self):
        return self._alarms

    def setAlarms_(self, alarms):
        self.set_alarms_calls += 1
        self._alarms = list(alarms)

    def startDateComponents(self):
        return self._start

    def setStartDateComponents_(self, value):
        self._start = value


def _alarm_times(item):
    return [
        rs.format_completed_at(a.absoluteDate()) if a.absoluteDate() else f"rel {a.relativeOffset()}"
        for a in item.alarms()
    ]


def test_update_redate_moves_alerts_and_a_matching_start_date():
    item = AlarmEK(
        due=_comps(2026, 11, 3, 9, 0), start=_comps(2026, 11, 3, 9, 0), calendar=CAL, item_id="r1",
        alarms=[StubAlarm(at=(2026, 11, 3, 9, 0)), StubAlarm(at=(2026, 11, 2, 9, 0)),
                StubAlarm(offset=-600.0), StubAlarm(location=True)],
    )
    store = WritableStore([item], [CAL])
    out = _writable(store).update("r1", ReminderEdit(due="2026-11-06T10:30"), "2026-11-03T09:00", None)
    assert _alarm_times(item)[:3] == ["2026-11-06T10:30", "2026-11-05T10:30", "rel -600.0"]
    assert item.alarms()[3].structuredLocation() is not None
    assert rs.format_due(item.startDateComponents()) == "2026-11-06T10:30"
    assert out.changes["alerts"] == {
        "before": ["2026-11-03T09:00", "2026-11-02T09:00", "relative -600s", "location"],
        "after": ["2026-11-06T10:30", "2026-11-05T10:30", "relative -600s", "location"],
    }
    assert out.changes["due"] == {"before": "2026-11-03T09:00", "after": "2026-11-06T10:30"}


def test_update_redate_without_absolute_alerts_leaves_alarms_untouched():
    item = AlarmEK(due=_comps(2026, 11, 3, 9, 0), start=_comps(2026, 11, 1), calendar=CAL,
                   item_id="r1", alarms=[StubAlarm(offset=-600.0)])
    store = WritableStore([item], [CAL])
    out = _writable(store).update("r1", ReminderEdit(due="2026-11-04T09:00"), "2026-11-03T09:00", None)
    assert item.set_alarms_calls == 0 and "alerts" not in out.changes
    assert rs.format_due(item.startDateComponents()) == "2026-11-01"  # a different start stays


def test_update_clear_due_drops_the_at_due_alert_or_refuses_on_others():
    item = AlarmEK(due=_comps(2026, 11, 3, 9, 0), start=_comps(2026, 11, 3, 9, 0), calendar=CAL,
                   item_id="r1", alarms=[StubAlarm(at=(2026, 11, 3, 9, 0)), StubAlarm(location=True)])
    store = WritableStore([item], [CAL])
    out = _writable(store).update("r1", ReminderEdit(clear_due=True), "2026-11-03T09:00", None)
    assert item.dueDateComponents() is None and item.startDateComponents() is None
    assert len(item.alarms()) == 1 and item.alarms()[0].structuredLocation() is not None
    assert out.changes["alerts"] == {"before": ["2026-11-03T09:00", "location"], "after": ["location"]}

    other = AlarmEK(due=_comps(2026, 11, 3, 9, 0), calendar=CAL, item_id="r2",
                    alarms=[StubAlarm(at=(2026, 11, 3, 9, 0)), StubAlarm(at=(2026, 11, 2, 9, 0))])
    store = WritableStore([other], [CAL])
    with pytest.raises(RemindersRefusal, match="other alert"):
        _writable(store).update("r2", ReminderEdit(clear_due=True), "2026-11-03T09:00", None)
    assert store.saved == [] and other.set_alarms_calls == 0


def test_update_due_edit_refused_when_alerts_cannot_be_read():
    class Unreadable(AlarmEK):
        def alarms(self):
            raise ValueError("NSInvalidArgumentException SECRET-TITLE")

    item = Unreadable(due=_comps(2026, 11, 3, 9, 0), calendar=CAL, item_id="r1")
    store = WritableStore([item], [CAL])
    with pytest.raises(RemindersRefusal, match="couldn't read") as e:
        _writable(store).update("r1", ReminderEdit(due="2026-11-04T09:00"), "2026-11-03T09:00", None)
    assert "SECRET" not in str(e.value) and store.saved == []
    _writable(store).update("r1", ReminderEdit(title="Renamed"), "2026-11-03T09:00", None)


def test_update_refuses_cross_account_move_and_move_out_of_never_complete():
    exchange = WritableCalendar("cal-x", "Reminders", source="Exchange")
    grocery = WritableCalendar("g", "Grocery List")
    item = WritableEK(calendar=CAL, item_id="r1")
    milk = WritableEK(calendar=grocery, item_id="r2")
    store = WritableStore([item, milk], [CAL, exchange, grocery])
    s = _writable(store)
    with pytest.raises(RemindersRefusal, match="another account"):
        s.update("r1", ReminderEdit(list_id="cal-x"), None, None)
    with pytest.raises(RemindersRefusal, match="never_complete_lists"):
        s.update("r2", ReminderEdit(list_id="cal-1"), None, None, GUARDED)
    assert store.saved == []


def test_update_refuses_rename_of_recurring_with_completed_copy_today():
    copy = WritableEK(
        title="Vitamins", completed=True, completion=_at(2026, 10, 27, 9, 0),
        calendar=CAL, item_id="copy-1",
    )
    daily = WritableEK(
        title="Vitamins", due=_comps(2026, 10, 28), recurring=True, calendar=CAL, item_id="d1"
    )
    store = WritableStore([daily], [CAL], answers={"completed": [copy]})
    with pytest.raises(RemindersRefusal, match="completed today"):
        _writable(store).update("d1", ReminderEdit(title="Vitamins (am)"), "2026-10-28", None)
    assert store.saved == []


def test_update_shift_keeps_the_alarm_action():
    """Review round 3 (#232): a shifted alert keeps its sound (or email action)."""
    original = StubAlarm(at=(2026, 11, 3, 9, 0), sound="Glass")
    item = AlarmEK(due=_comps(2026, 11, 3, 9, 0), calendar=CAL, item_id="r1", alarms=[original])
    _writable(WritableStore([item], [CAL])).update(
        "r1", ReminderEdit(due="2026-11-05T09:00"), "2026-11-03T09:00", None
    )
    (moved,) = item.alarms()
    assert moved.soundName() == "Glass" and moved is not original
    assert _alarm_times(item) == ["2026-11-05T09:00"]
    assert rs.format_completed_at(original.absoluteDate()) == "2026-11-03T09:00"
