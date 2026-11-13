"""Apple Reminders access through EventKit, in-process (issue #171).

EventKit talks to the Reminders daemon over XPC: no WindowServer, keystrokes or
clipboard, so it works headless under launchd and with the screen locked.
Access is checked against the process's responsible app (AppleNotesMCP.app
under the remote LaunchAgent). This module only reads the grant; it never asks
for one. The wrapper's ``--probe-reminders`` mode does that, once, while the owner
is at the Mac.

Writes are exactly three: completing an existing reminder, creating a new one
(Phase 2), and editing one's title, due date, notes, list or priority (#232),
each after the guards in ``guards.py`` pass, under the same lock as the reads.
Nothing here removes or uncompletes a reminder, touches its recurrence, or
changes a list; alerts change only by moving with the due date.
"""

import datetime as dt
import sys
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable, Optional, Sequence

import EventKit
import objc
from Foundation import NSDate, NSDateComponents

from apple_notes import demo_mode

from . import guards
from .grant import STATUS_WORDS

FETCH_TIMEOUT_S = 20.0
# NSDateComponentUndefined is NSIntegerMax.
_UNDEFINED = sys.maxsize


@dataclass(frozen=True)
class ReminderList:
    id: str
    name: str
    source: str


@dataclass(frozen=True)
class Reminder:
    id: str
    title: str
    list_id: str
    list: str
    due: Optional[str]
    recurring: bool
    completed: bool
    completed_at: Optional[str]
    notes: Optional[str]
    priority: str = "none"


class RemindersAccessError(Exception):
    """This process's app has no full Reminders access."""

    def __init__(self, status: str):
        super().__init__(f"Reminders access: {status}")
        self.status = status


class RemindersTimeoutError(Exception):
    """EventKit did not answer a fetch in time."""


class RemindersNotFoundError(Exception):
    """No reminder (or list) has that id."""


class RemindersWriteError(Exception):
    """EventKit refused a save. The message carries only the NSError domain and
    code, never the reminder's text."""


@dataclass(frozen=True)
class Completion:
    id: str
    list: str
    recurring: bool
    completed_due: Optional[str]
    next_due: Optional[str]


@dataclass(frozen=True)
class Update:
    id: str
    list: str
    list_id: str
    # {field: {"before": ..., "after": ...}}, from the saved reminder.
    changes: dict


# EventKit priorities: 0 none, 1-4 high, 5 medium, 6-9 low. Setting uses the
# values the Reminders app itself writes.
PRIORITY_VALUES = {"none": 0, "high": 1, "medium": 5, "low": 9}


def priority_word(value) -> str:
    v = int(value or 0)
    if 1 <= v <= 4:
        return "high"
    if v == 5:
        return "medium"
    if 6 <= v <= 9:
        return "low"
    return "none"


def _defined(value) -> bool:
    return value is not None and value != _UNDEFINED


def format_due(components) -> Optional[str]:
    """``YYYY-MM-DD``, or ``YYYY-MM-DDTHH:MM`` when the due date has a time."""
    if components is None or not _defined(components.year()):
        return None
    day = f"{components.year():04d}-{components.month():02d}-{components.day():02d}"
    if not _defined(components.hour()):
        return day
    minute = components.minute() if _defined(components.minute()) else 0
    return f"{day}T{components.hour():02d}:{minute:02d}"


def format_completed_at(nsdate) -> Optional[str]:
    """Local ``YYYY-MM-DDTHH:MM`` for an NSDate, or None."""
    if nsdate is None:
        return None
    when = dt.datetime.fromtimestamp(nsdate.timeIntervalSince1970())
    return when.strftime("%Y-%m-%dT%H:%M")


def due_components(due: str):
    """NSDateComponents for a ``YYYY-MM-DD[THH:MM]`` due date, with no calendar
    or time zone (a floating date, as Reminders stores one set on the phone)."""
    day, at = guards.parse_due(due)
    c = NSDateComponents.alloc().init()
    c.setYear_(day.year)
    c.setMonth_(day.month)
    c.setDay_(day.day)
    if at is not None:
        c.setHour_(at.hour)
        c.setMinute_(at.minute)
    return c


def reminder_from_ek(r) -> Reminder:
    calendar = r.calendar()
    return Reminder(
        id=str(r.calendarItemIdentifier()),
        title=str(r.title() or ""),
        list_id=str(calendar.calendarIdentifier()),
        list=str(calendar.title()),
        due=format_due(r.dueDateComponents()),
        recurring=bool(r.hasRecurrenceRules()),
        completed=bool(r.isCompleted()),
        completed_at=format_completed_at(r.completionDate()),
        notes=str(r.notes()) if r.notes() else None,
        priority=priority_word(r.priority()),
    )


def _new_event_store():
    return EventKit.EKEventStore.alloc().init()


def _new_reminder(store):
    return EventKit.EKReminder.reminderWithEventStore_(store)


def _system_status() -> int:
    return EventKit.EKEventStore.authorizationStatusForEntityType_(
        EventKit.EKEntityTypeReminder
    )


class EventKitStore:
    """One EKEventStore per process, shared by the remote server's worker threads.

    Reads are serialized under one lock (EventKit's thread-safety for a shared
    store is undocumented), and the wait for that lock is bounded by the fetch
    timeout, so a stalled fetch can't queue every other call forever. Each read
    runs in an autorelease pool: the anyio worker threads are long-lived and
    would otherwise accumulate autoreleased ObjC objects.
    """

    def __init__(
        self,
        event_store=None,
        status_fn: Optional[Callable[[], int]] = None,
        fetch_timeout: float = FETCH_TIMEOUT_S,
        store_factory: Optional[Callable[[], object]] = None,
        today_fn: Callable[[], dt.date] = dt.date.today,
        new_reminder: Optional[Callable[[object], object]] = None,
    ):
        # An injected event_store is kept as is; otherwise the store is built
        # lazily and rebuilt after access was lost (a store created before a
        # revoke-and-regrant keeps answering without the new grant).
        self._fixed_store = event_store
        self._event_store = event_store
        self._store_factory = store_factory or _new_event_store
        self._status_fn = status_fn or _system_status
        self._fetch_timeout = fetch_timeout
        self._today = today_fn
        self._new_reminder = new_reminder or _new_reminder
        self._lock = threading.Lock()

    def authorization_status(self) -> str:
        demo_mode.require_live("Apple Reminders")
        raw = int(self._status_fn())
        return STATUS_WORDS.get(raw, f"unknown-{raw}")

    @contextmanager
    def _session(self):
        """The shared store, under the one lock and an autorelease pool.

        Reads and writes both take it, so a write's guard checks and its save
        can't interleave with another call."""
        demo_mode.require_live("Apple Reminders")
        if not self._lock.acquire(timeout=self._fetch_timeout):
            raise RemindersTimeoutError(
                f"Reminders busy for {self._fetch_timeout:.0f}s with another read"
            )
        try:
            with objc.autorelease_pool():
                status = self.authorization_status()
                if status != "granted":
                    if self._fixed_store is None:
                        self._event_store = None
                    raise RemindersAccessError(status)
                if self._event_store is None:
                    self._event_store = self._store_factory()
                store = self._event_store
                # Pick up changes made on the owner's other devices since the last read.
                store.refreshSourcesIfNecessary()
                yield store
        finally:
            self._lock.release()

    def _calendars(self, store):
        return list(store.calendarsForEntityType_(EventKit.EKEntityTypeReminder) or [])

    def lists(self) -> list[ReminderList]:
        with self._session() as store:
            return [
                ReminderList(
                    id=str(c.calendarIdentifier()),
                    name=str(c.title()),
                    source=str(c.source().title()),
                )
                for c in self._calendars(store)
            ]

    def reminders(
        self,
        list_ids: Optional[Sequence[str]],
        include_completed: bool,
        completed_since: Optional[dt.datetime] = None,
    ) -> list[Reminder]:
        """Open reminders, plus (with ``include_completed``) those completed since
        ``completed_since``. Never every completed reminder ever made: years of
        history would time out or blow the result-size cap."""
        with self._session() as store:
            calendars = None
            if list_ids is not None:
                wanted = set(list_ids)
                calendars = [
                    c
                    for c in self._calendars(store)
                    if str(c.calendarIdentifier()) in wanted
                ]
                if not calendars:
                    # An empty calendar list would mean "every list" to EventKit.
                    return []
            predicates = [
                store.predicateForIncompleteRemindersWithDueDateStarting_ending_calendars_(
                    None, None, calendars
                )
            ]
            if include_completed:
                predicates.append(
                    _completed_predicate(store, completed_since, calendars)
                )
            items: list[Reminder] = []
            for predicate in predicates:
                items.extend(self._fetch(store, predicate))
            return items

    def complete(
        self,
        reminder_id: str,
        expected_due: Optional[str],
        never_complete_lists: Sequence[str],
    ) -> Completion:
        """Complete one reminder if guards 1-4 pass; report what moved."""
        with self._session() as store:
            item = self._reminder(store, reminder_id)
            calendar = item.calendar()
            r = reminder_from_ek(item)
            # Guards 1-3 first: they are instant and name their remote.json key.
            guards.check_completable(r, expected_due, never_complete_lists, None)
            _check_writable(calendar, r.list, guards.NOT_COMPLETED)
            if r.recurring:
                today = self._today()
                done = self._fetch(
                    store,
                    _completed_predicate(
                        store, dt.datetime.combine(today, dt.time()), [calendar]
                    ),
                )
                guards.check_completable(
                    r,
                    expected_due,
                    never_complete_lists,
                    guards.completed_today_copy(r, done, today),
                )
            item.setCompleted_(True)
            _save(store, item)
            # A recurring reminder stays open under the same id with its due
            # date moved to the next occurrence (verified, design doc).
            after = store.calendarItemWithIdentifier_(reminder_id)
            next_due = None
            if r.recurring and after is not None and not after.isCompleted():
                next_due = format_due(after.dueDateComponents())
            return Completion(
                id=r.id,
                list=r.list,
                recurring=r.recurring,
                completed_due=r.due,
                next_due=next_due,
            )

    def create(
        self,
        title: str,
        list_id: str,
        due: Optional[str],
        notes: Optional[str],
    ) -> Reminder:
        """Create one reminder (no recurrence, alarm or location) in ``list_id``."""
        with self._session() as store:
            calendar = store.calendarWithIdentifier_(list_id)
            if calendar is None:
                raise RemindersNotFoundError("no list with that id")
            _check_writable(calendar, str(calendar.title()), guards.NOT_CREATED)
            open_items = self._fetch(
                store,
                store.predicateForIncompleteRemindersWithDueDateStarting_ending_calendars_(
                    None, None, [calendar]
                ),
            )
            guards.check_no_open_duplicate(title, open_items)
            item = self._new_reminder(store)
            item.setTitle_(title)
            item.setCalendar_(calendar)
            if notes:
                item.setNotes_(notes)
            if due is not None:
                item.setDueDateComponents_(due_components(due))
            _save(store, item)
            return reminder_from_ek(item)

    def update(
        self,
        reminder_id: str,
        edit: guards.ReminderEdit,
        expected_due: Optional[str],
        expected_title: Optional[str],
        never_complete_lists: Sequence[str] = (),
    ) -> Update:
        """Change the fields ``edit`` names on one open reminder (#232).

        ``edit`` has passed ``guards.check_edit_args``, and its ``list_id`` is
        already resolved against ``create_lists``. Before and after come from
        the reminder as saved."""
        with self._session() as store:
            item = self._reminder(store, reminder_id)
            r = reminder_from_ek(item)
            guards.check_updatable(r, expected_due, expected_title)
            _check_writable(item.calendar(), r.list, guards.NOT_UPDATED)
            target = item.calendar()
            if edit.list_id is not None:
                target = store.calendarWithIdentifier_(edit.list_id)
                if target is None:
                    raise RemindersNotFoundError("no list with that id")
                _check_writable(target, str(target.title()), guards.NOT_UPDATED)
            fields = guards.changed_fields(r, edit)
            new_due = None if edit.clear_due else edit.due
            alerts = planned = None
            if "due" in fields:
                # Alerts move with the due date (#232); planned before any write.
                raw_alarms = _read_alarms(item)
                alerts = [_alert_from_ek(a) for a in raw_alarms]
                planned = guards.plan_alerts(alerts, r.due, new_due)
            if "list" in fields:
                guards.check_move(
                    r,
                    never_complete_lists,
                    same_account=_account(target) == _account(item.calendar()),
                )
            if r.recurring and ("title" in fields or "list" in fields):
                today = self._today()
                done = self._fetch(
                    store,
                    _completed_predicate(
                        store, dt.datetime.combine(today, dt.time()), [item.calendar()]
                    ),
                )
                guards.check_completion_link(
                    r, guards.completed_today_copy(r, done, today)
                )
            if "title" in fields or "list" in fields:
                open_items = self._fetch(
                    store,
                    store.predicateForIncompleteRemindersWithDueDateStarting_ending_calendars_(
                        None, None, [target]
                    ),
                )
                guards.check_no_open_duplicate(
                    edit.title if edit.title is not None else r.title,
                    open_items,
                    guards.NOT_UPDATED,
                    ignore_id=r.id,
                )
            if "title" in fields:
                item.setTitle_(edit.title)
            if "due" in fields:
                start = item.startDateComponents()
                item.setDueDateComponents_(
                    None if new_due is None else due_components(new_due)
                )
                if start is not None and format_due(start) == r.due:
                    # Reminders.app keeps the start date at the due date; a
                    # relative alert may be anchored to it, so it moves too.
                    item.setStartDateComponents_(
                        None if new_due is None else due_components(new_due)
                    )
                if planned != alerts:
                    item.setAlarms_(
                        [
                            _alarm_to_ek(raw, after)
                            for raw, after in zip(raw_alarms, planned)
                            if after is not None
                        ]
                    )
            if "notes" in fields:
                item.setNotes_(None if edit.clear_notes else edit.notes)
            if "list" in fields:
                item.setCalendar_(target)
            if "priority" in fields:
                item.setPriority_(PRIORITY_VALUES[edit.priority])
            _save(store, item)
            after = reminder_from_ek(item)
            changes = guards.describe_changes(r, after, fields)
            if planned != alerts:
                changes["alerts"] = {
                    "before": [guards.describe_alert(a) for a in alerts],
                    "after": [guards.describe_alert(a) for a in planned if a is not None],
                }
            return Update(
                id=after.id,
                list=after.list,
                list_id=after.list_id,
                changes=changes,
            )

    def _reminder(self, store, reminder_id: str):
        item = store.calendarItemWithIdentifier_(reminder_id)
        if item is None or not item.isKindOfClass_(EventKit.EKReminder):
            raise RemindersNotFoundError("no reminder with that id")
        return item

    def _fetch(self, store, predicate) -> list[Reminder]:
        done = threading.Event()
        box: dict = {}

        def on_fetch(fetched):
            # Convert on EventKit's callback thread: EKReminder objects are not
            # meant to cross threads.
            try:
                with objc.autorelease_pool():
                    box["items"] = [reminder_from_ek(r) for r in (fetched or [])]
            except Exception as e:  # surfaced on the calling thread below
                box["error"] = e
            finally:
                done.set()

        request = store.fetchRemindersMatchingPredicate_completion_(predicate, on_fetch)
        if not done.wait(self._fetch_timeout):
            if request is not None:
                store.cancelFetchRequest_(request)
            raise RemindersTimeoutError(
                f"no answer from Reminders within {self._fetch_timeout:.0f}s"
            )
        if "error" in box:
            raise box["error"]
        return box["items"]


def _completed_predicate(store, since: Optional[dt.datetime], calendars):
    start = (
        NSDate.dateWithTimeIntervalSince1970_(since.timestamp())
        if since is not None
        else None
    )
    return store.predicateForCompletedRemindersWithCompletionDateStarting_ending_calendars_(
        start, None, calendars
    )


def _read_alarms(item) -> list:
    """The reminder's EKAlarms. EventKit can raise reading them (seen live: a
    start date plus several alarms, read from a second store); then the due
    date is left alone rather than moved without its alerts."""
    try:
        return list(item.alarms() or [])
    except Exception:
        pass
    # Raised outside the except block, so no exception text rides along.
    raise guards.RemindersRefusal(
        "couldn't read that reminder's alerts, so its due date can't be moved "
        "with them. Change it in the Reminders app; " + guards.NOT_UPDATED
    )


def _alert_from_ek(a) -> guards.Alert:
    if a.structuredLocation() is not None:
        return guards.Alert("location")
    at = a.absoluteDate()
    if at is not None:
        return guards.Alert(
            "absolute", at=dt.datetime.fromtimestamp(at.timeIntervalSince1970())
        )
    return guards.Alert("relative", offset=float(a.relativeOffset()))


# The alarm actions a shifted alert carries over (macOS EKAlarm properties).
_ALARM_ACTIONS = (("soundName", "setSoundName_"), ("emailAddress", "setEmailAddress_"))


def _alarm_to_ek(raw, planned: guards.Alert):
    """A shifted absolute alert is a new EKAlarm with ``raw``'s sound or email
    action; any other is copied as is. Not a copy with a new date: a copy keeps
    the original's identity, and a later removal brought the old time back
    (live, re-date then clear in one store). Setting the whole list is what
    persists."""
    if planned.kind != "absolute" or planned.at == _alert_from_ek(raw).at:
        return raw.copy()
    alarm = EventKit.EKAlarm.alarmWithAbsoluteDate_(
        NSDate.dateWithTimeIntervalSince1970_(planned.at.timestamp())
    )
    for getter, setter in _ALARM_ACTIONS:
        read = getattr(raw, getter, None)
        value = read() if callable(read) else None
        if value:
            getattr(alarm, setter)(value)
    return alarm


def _account(calendar) -> str:
    return str(calendar.source().sourceIdentifier())


def _check_writable(calendar, name: str, nothing_done: str) -> None:
    if not calendar.allowsContentModifications():
        raise guards.RemindersRefusal(
            f'list "{name}" is read-only in Reminders; {nothing_done}'
        )


def _save(store, item) -> None:
    failure = None
    try:
        ok, err = store.saveReminder_commit_error_(item, True, None)
    except Exception as e:
        failure = f"raised {type(e).__name__}"
    else:
        if not ok:
            failure = (
                f"{err.domain()} {err.code()}" if err is not None else "no error given"
            )
    if failure is None:
        return
    _discard(store, item)
    # Raised outside the except blocks, so no exception (whose text could
    # quote the reminder) rides along as the cause or context.
    raise RemindersWriteError(f"save failed: {failure}")


def _discard(store, item) -> None:
    """Undo a failed save: the object's change, and anything the store still
    holds pending, so no later commit can save what was reported as not saved.
    Resetting is safe here: no EventKit object outlives a session, and the
    lock keeps every other call out. Best effort: a failure here must not
    replace the write error."""
    try:
        item.rollback()
        store.reset()
    except Exception:
        pass
