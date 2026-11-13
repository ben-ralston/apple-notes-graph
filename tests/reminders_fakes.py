"""In-memory stand-in for EventKitStore, shared by the reminders tests (#171)."""

import asyncio
import json

import dataclasses

from apple_notes_mcp.reminders import guards
from apple_notes_mcp.reminders.store import (
    Completion,
    PRIORITY_VALUES,
    Reminder,
    ReminderList,
    RemindersAccessError,
    RemindersNotFoundError,
    RemindersWriteError,
    Update,
)

LISTS = [
    ReminderList(id="a", name="Reminders", source="iCloud"),
    ReminderList(id="b", name="Reminders", source="Exchange"),
    ReminderList(id="g", name="Grocery List", source="iCloud"),
]


def _rem(rid, title, list_id, due=None, completed=False, notes=None, recurring=False):
    name = next(lst.name for lst in LISTS if lst.id == list_id)
    return Reminder(
        id=rid,
        title=title,
        list_id=list_id,
        list=name,
        due=due,
        recurring=recurring,
        completed=completed,
        completed_at="2026-10-20T10:00" if completed else None,
        notes=notes,
    )


REMS = [
    _rem("r1", "Pay rent", "a", due="2026-10-24", recurring=True),
    _rem("r2", "Call mom", "b", due="2026-10-27T09:00"),
    _rem("r3", "Undated", "a", notes="x" * 2500),
    _rem("r4", "Milk", "g", due="2026-10-30"),
    _rem("r5", "Done thing", "a", due="2026-10-20", completed=True),
]


class FakeStore:
    """Writes apply the real guards, like EventKitStore does under its lock.

    Completing a recurring reminder moves its due date to ``next_due``.
    ``completed_today`` stands in for the completed copy EventKit leaves.
    """

    def __init__(self, lists=LISTS, reminders=REMS, status="granted"):
        self._lists, self._reminders, self._status = list(lists), list(reminders), status
        self.read_only_lists = set()
        self.completed_today = None
        self.next_due = "2026-11-24"
        self.fail_writes = False
        self.completed, self.created, self.updated = [], [], []

    def authorization_status(self):
        return self._status

    def _check(self):
        if self._status != "granted":
            raise RemindersAccessError(self._status)

    def lists(self):
        self._check()
        return list(self._lists)

    def reminders(self, list_ids, include_completed, completed_since=None):
        self._check()
        return [
            r
            for r in self._reminders
            if (list_ids is None or r.list_id in list_ids)
            and (include_completed or not r.completed)
        ]


    def _writable(self, list_id, name, nothing_done):
        if list_id in self.read_only_lists:
            raise guards.RemindersRefusal(
                f'list "{name}" is read-only in Reminders; {nothing_done}'
            )
        if self.fail_writes:
            raise RemindersWriteError("save failed: EKErrorDomain 17")

    def complete(self, reminder_id, expected_due, never_complete_lists):
        self._check()
        r = next((r for r in self._reminders if r.id == reminder_id), None)
        if r is None:
            raise RemindersNotFoundError("no reminder with that id")
        guards.check_completable(
            r,
            expected_due,
            never_complete_lists,
            self.completed_today if r.recurring else None,
        )
        self._writable(r.list_id, r.list, guards.NOT_COMPLETED)
        after = (
            dataclasses.replace(r, due=self.next_due)
            if r.recurring
            else dataclasses.replace(r, completed=True, completed_at="2026-10-27T12:00")
        )
        self._reminders[self._reminders.index(r)] = after
        self.completed.append(reminder_id)
        return Completion(
            id=r.id,
            list=r.list,
            recurring=r.recurring,
            completed_due=r.due,
            next_due=self.next_due if r.recurring else None,
        )

    def create(self, title, list_id, due, notes):
        self._check()
        lst = next((lst for lst in self._lists if lst.id == list_id), None)
        if lst is None:
            raise RemindersNotFoundError("no list with that id")
        self._writable(list_id, lst.name, guards.NOT_CREATED)
        guards.check_no_open_duplicate(
            title, [r for r in self._reminders if r.list_id == list_id]
        )
        new = Reminder(
            id=f"new-{len(self.created) + 1}",
            title=title,
            list_id=list_id,
            list=lst.name,
            due=due,
            recurring=False,
            completed=False,
            completed_at=None,
            notes=notes,
        )
        self._reminders.append(new)
        self.created.append(new)
        return new

    def update(self, reminder_id, edit, expected_due, expected_title, never_complete_lists=()):
        """FakeStore.update: the real guards, in EventKitStore.update's order."""
        self._check()
        r = next((r for r in self._reminders if r.id == reminder_id), None)
        if r is None:
            raise RemindersNotFoundError("no reminder with that id")
        guards.check_updatable(r, expected_due, expected_title)
        self._writable(r.list_id, r.list, guards.NOT_UPDATED)
        target = r
        if edit.list_id is not None:
            lst = next((lst for lst in self._lists if lst.id == edit.list_id), None)
            if lst is None:
                raise RemindersNotFoundError("no list with that id")
            self._writable(lst.id, lst.name, guards.NOT_UPDATED)
            target = dataclasses.replace(r, list_id=lst.id, list=lst.name)
        fields = guards.changed_fields(r, edit)
        if "list" in fields:
            source = next(x.source for x in self._lists if x.id == r.list_id)
            guards.check_move(r, never_complete_lists, same_account=lst.source == source)
        if r.recurring and ("title" in fields or "list" in fields):
            guards.check_completion_link(r, self.completed_today)
        if "title" in fields or "list" in fields:
            guards.check_no_open_duplicate(
                edit.title if edit.title is not None else r.title,
                [o for o in self._reminders if o.list_id == target.list_id],
                guards.NOT_UPDATED,
                ignore_id=r.id,
            )
        after = target
        if "title" in fields:
            after = dataclasses.replace(after, title=edit.title)
        if "due" in fields:
            after = dataclasses.replace(after, due=None if edit.clear_due else edit.due)
        if "notes" in fields:
            after = dataclasses.replace(after, notes=None if edit.clear_notes else edit.notes)
        if "priority" in fields:
            assert edit.priority in PRIORITY_VALUES
            after = dataclasses.replace(after, priority=edit.priority)
        self._reminders[self._reminders.index(r)] = after
        self.updated.append(reminder_id)
        return Update(
            id=after.id,
            list=after.list,
            list_id=after.list_id,
            changes=guards.describe_changes(r, after, fields),
        )


def call(mcp, name, arguments=None):
    """Call a FastMCP tool and return its JSON result as a dict."""
    result = asyncio.run(mcp.call_tool(name, arguments or {}))
    content = result[0] if isinstance(result, tuple) else result
    return json.loads(content[0].text)
