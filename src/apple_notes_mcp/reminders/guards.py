"""Server-side guards for Reminders writes (issue #171, Phase 2).

Pure functions over the store's dataclasses, shared by the EventKit store
(which runs them under the same lock as the save) and the tests' fake store.
Every refusal says what stopped it and, where one applies, the remote.json key
that controls it, so a restriction the owner has forgotten is traceable from the
error alone. Messages never carry a reminder's title or notes: the list name,
id and due dates are enough, and refusals can reach logs.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Iterable, Optional, Sequence

if TYPE_CHECKING:
    from .store import Reminder, ReminderList

TITLE_MAX_CHARS = 500
CREATE_NOTES_MAX_CHARS = 2000

NOT_COMPLETED = "nothing was completed."
NOT_CREATED = "nothing was created."
NOT_UPDATED = "nothing was changed."

_DUE = re.compile(r"\d{4}-\d{2}-\d{2}(T\d{2}:\d{2})?")


class RemindersRefusal(Exception):
    """A guard refused the write; nothing was saved. The message is for the caller."""


def _norm(name: str) -> str:
    """How list names and titles compare: config and Reminders alike, so a
    stray space on either side can't switch a guard off."""
    return name.strip().casefold()


def _time_of_day(due: Optional[str]) -> str:
    return due[11:] if due and len(due) > 10 else ""


def _due_word(due: Optional[str]) -> str:
    return "none" if due is None else repr(due)


def check_completable(
    r: Reminder,
    expected_due: Optional[str],
    never_complete_lists: Sequence[str],
    completed_today: Optional[Reminder],
) -> None:
    """Guards 1-4 of the design, in its order. Raises RemindersRefusal."""
    if _norm(r.list) in {_norm(name) for name in never_complete_lists}:
        raise RemindersRefusal(
            f'list "{r.list}" is in reminders.never_complete_lists (remote.json); '
            + NOT_COMPLETED
        )
    if r.completed:
        raise RemindersRefusal("that reminder is already completed; " + NOT_COMPLETED)
    if expected_due != r.due:
        # Compare-and-swap: a recurring reminder's due date moves on completion,
        # so a repeat call in the same pass carries a stale expected_due.
        raise RemindersRefusal(
            f"expected_due {_due_word(expected_due)} doesn't match the reminder's "
            f"current due {_due_word(r.due)}; it may already have been completed. "
            "Read it again with reminders_list before retrying; " + NOT_COMPLETED
        )
    if r.recurring and completed_today is not None:
        # Across passes the due date matches again, so only the completed copy
        # left by an earlier completion today shows the tick was applied.
        raise RemindersRefusal(
            "that recurring reminder was already completed today "
            f"(at {completed_today.completed_at}); completing it again would skip "
            f"its next occurrence ({r.due}). Nothing was completed."
        )


def completed_today_copy(
    r: Reminder, completed: Iterable[Reminder], today: dt.date
) -> Optional[Reminder]:
    """The completed copy a recurring reminder left today, if any.

    EventKit gives that copy a new id and no link back, so it is matched by
    list, exact title and the due date's time of day (or both having none), so
    a same-titled reminder at another time ("Take meds" 8:00 vs 20:00) doesn't
    count.
    """
    day = today.isoformat()
    for c in completed:
        if (
            c.completed
            and c.id != r.id
            and c.list_id == r.list_id
            and c.title == r.title
            and _time_of_day(c.due) == _time_of_day(r.due)
            and (c.completed_at or "")[:10] == day
        ):
            return c
    return None


def parse_due(
    due: str, nothing_done: str = NOT_CREATED
) -> tuple[dt.date, Optional[dt.time]]:
    """``YYYY-MM-DD`` or ``YYYY-MM-DDTHH:MM`` (the format reminders_list returns)."""
    try:
        if not _DUE.fullmatch(due):
            raise ValueError(due)
        day = dt.date.fromisoformat(due[:10])
        at = dt.time.fromisoformat(due[11:]) if len(due) > 10 else None
    except ValueError:
        raise RemindersRefusal(
            "due must look like 2026-10-27 or 2026-10-27T09:00; " + nothing_done
        ) from None
    return day, at


def _clean_title(title: str, nothing_done: str) -> str:
    title = title.strip()
    if not title:
        raise RemindersRefusal("title is empty; " + nothing_done)
    if len(title) > TITLE_MAX_CHARS:
        raise RemindersRefusal(
            f"title is over {TITLE_MAX_CHARS} characters; " + nothing_done
        )
    return title


def _check_notes_cap(notes: Optional[str], nothing_done: str) -> None:
    if notes is not None and len(notes) > CREATE_NOTES_MAX_CHARS:
        raise RemindersRefusal(
            f"notes are over {CREATE_NOTES_MAX_CHARS} characters; " + nothing_done
        )


def check_creatable(title: str, notes: Optional[str]) -> str:
    """The stripped title, or a refusal naming the limit (never the text)."""
    title = _clean_title(title, NOT_CREATED)
    _check_notes_cap(notes, NOT_CREATED)
    return title


def resolve_create_list(
    list_arg: str,
    lists: Sequence[ReminderList],
    create_lists: Optional[Sequence[str]],
    nothing_done: str = NOT_CREATED,
) -> ReminderList:
    """The one list ``list_arg`` names (an id, or a case-insensitive name).

    Also the target of a move (reminders_update): ``create_lists`` limits where
    reminders may land, whether new or moved."""
    matches = [lst for lst in lists if lst.id == list_arg]
    if not matches:
        wanted = _norm(list_arg)
        matches = [lst for lst in lists if _norm(lst.name) == wanted]
    if not matches:
        names = ", ".join(sorted({lst.name for lst in lists}))
        raise RemindersRefusal(
            f"no Reminders list named {list_arg!r} (lists: {names}); " + nothing_done
        )
    if len(matches) > 1:
        raise RemindersRefusal(
            f'{len(matches)} lists are named "{matches[0].name}"; pass its id '
            "from reminders_lists. " + nothing_done.capitalize()
        )
    target = matches[0]
    if create_lists is not None and _norm(target.name) not in {
        _norm(name) for name in create_lists
    }:
        raise RemindersRefusal(
            f'list "{target.name}" is not in reminders.create_lists (remote.json); '
            + nothing_done
        )
    return target


def check_no_open_duplicate(
    title: str,
    open_in_list: Iterable[Reminder],
    nothing_done: str = NOT_CREATED,
    ignore_id: Optional[str] = None,
) -> None:
    """Refuse a second open reminder with the same title (a retried create, or
    a rename or move onto one). ``ignore_id`` is the reminder being edited."""
    wanted = _norm(title)
    for r in open_in_list:
        if r.id != ignore_id and not r.completed and _norm(r.title) == wanted:
            raise RemindersRefusal(
                f'an open reminder with the same title is already in list "{r.list}" '
                f"(id {r.id}); " + nothing_done
            )


# --- updating (#232) ----------------------------------------------------------

PRIORITIES = ("none", "low", "medium", "high")
# The order changed_fields reports fields in, and the Reminder attribute each reads.
_EDIT_FIELDS = ("title", "due", "notes", "list", "priority")


@dataclass(frozen=True)
class ReminderEdit:
    """What reminders_update was asked to change. None (or False) leaves a
    field alone; ``clear_due`` / ``clear_notes`` remove one. ``list_id`` is the
    target list's id once the tool has resolved it."""

    title: Optional[str] = None
    due: Optional[str] = None
    clear_due: bool = False
    notes: Optional[str] = None
    clear_notes: bool = False
    list_id: Optional[str] = None
    priority: Optional[str] = None


def _wanted(edit: ReminderEdit) -> dict[str, bool]:
    """Which fields the edit asks about (the one table both checks read)."""
    return {
        "title": edit.title is not None,
        "due": edit.due is not None or edit.clear_due,
        "notes": edit.notes is not None or edit.clear_notes,
        "list": edit.list_id is not None,
        "priority": edit.priority is not None,
    }


def check_edit_args(edit: ReminderEdit) -> ReminderEdit:
    """The edit with its title stripped and priority lower-cased, or a refusal.

    Argument checks only: nothing here reads the reminder."""
    if not any(_wanted(edit).values()):
        raise RemindersRefusal(
            "nothing to change: pass at least one of title, due, clear_due, notes, "
            "clear_notes, list or priority; " + NOT_UPDATED
        )
    if edit.due is not None and edit.clear_due:
        raise RemindersRefusal("pass due or clear_due, not both; " + NOT_UPDATED)
    if edit.notes is not None and edit.clear_notes:
        raise RemindersRefusal("pass notes or clear_notes, not both; " + NOT_UPDATED)
    title = None if edit.title is None else _clean_title(edit.title, NOT_UPDATED)
    if edit.due is not None:
        parse_due(edit.due, NOT_UPDATED)
    if edit.notes == "":
        raise RemindersRefusal(
            "notes are empty; pass clear_notes=true to remove them. Nothing was changed."
        )
    _check_notes_cap(edit.notes, NOT_UPDATED)
    priority = None
    if edit.priority is not None:
        priority = edit.priority.strip().casefold()
        if priority not in PRIORITIES:
            raise RemindersRefusal(
                f"priority must be one of {', '.join(PRIORITIES)}; " + NOT_UPDATED
            )
    return replace(edit, title=title, priority=priority)


def check_updatable(
    r: Reminder, expected_due: Optional[str], expected_title: Optional[str]
) -> None:
    """Refuse a completed reminder, or one that changed since the caller read it."""
    if r.completed:
        raise RemindersRefusal("that reminder is already completed; " + NOT_UPDATED)
    if expected_due != r.due:
        raise RemindersRefusal(
            f"expected_due {_due_word(expected_due)} doesn't match the reminder's "
            f"current due {_due_word(r.due)}; it changed since you read it (or an "
            "earlier call already applied this edit). Read it again with "
            "reminders_list before retrying; " + NOT_UPDATED
        )
    if expected_title is not None and expected_title != r.title:
        raise RemindersRefusal(
            "expected_title doesn't match the reminder's current title; it changed "
            "since you read it (or an earlier call already applied this edit). Read "
            "it again with reminders_list before retrying; " + NOT_UPDATED
        )


def changed_fields(r: Reminder, edit: ReminderEdit) -> tuple[str, ...]:
    """The fields ``edit`` really changes on ``r``, in ``_EDIT_FIELDS`` order.

    Refuses a due-date change on a recurring reminder, and an edit that changes
    nothing (every value passed is already the current one)."""
    wants = _wanted(edit)
    new = {
        "title": edit.title,
        "due": None if edit.clear_due else edit.due,
        "notes": None if edit.clear_notes else edit.notes,
        "list": edit.list_id,
        "priority": edit.priority,
    }
    current = {
        "title": r.title,
        "due": r.due,
        "notes": r.notes,
        "list": r.list_id,
        "priority": r.priority,
    }
    fields = tuple(f for f in _EDIT_FIELDS if wants[f] and new[f] != current[f])
    if "due" in fields and r.recurring:
        raise RemindersRefusal(
            "that reminder repeats, and EventKit keeps one reminder per series: "
            "a new due date would move the whole series, not just this occurrence. "
            "reminders_update doesn't change due dates on recurring reminders; "
            "change it in the Reminders app; " + NOT_UPDATED
        )
    if not fields:
        raise RemindersRefusal("that edit changes nothing; " + NOT_UPDATED)
    return fields


def check_move(
    r: Reminder, never_complete_lists: Sequence[str], same_account: bool
) -> None:
    """Refuse a move out of a never-complete list (once out, it could be
    completed) or into another account (EventKit may give it a new id or drop
    what that account can't hold, such as subtasks)."""
    if _norm(r.list) in {_norm(name) for name in never_complete_lists}:
        raise RemindersRefusal(
            f'list "{r.list}" is in reminders.never_complete_lists (remote.json), so '
            "its reminders can't be moved out; " + NOT_UPDATED
        )
    if not same_account:
        raise RemindersRefusal(
            "that list is in another account; moving a reminder between accounts can "
            "change its id or drop its subtasks. Move it in the Reminders app; "
            + NOT_UPDATED
        )


def check_completion_link(r: Reminder, completed_today: Optional[Reminder]) -> None:
    """Refuse renaming or moving a recurring reminder that was completed today:
    its completed copy is found by title and list (``completed_today_copy``), so
    the edit would hide that tick and a second completion would skip an
    occurrence."""
    if r.recurring and completed_today is not None:
        raise RemindersRefusal(
            "that recurring reminder was already completed today; renaming or moving "
            "it now would let it be completed again and skip an occurrence. Try "
            "tomorrow; " + NOT_UPDATED
        )


def describe_changes(
    before: Reminder, after: Reminder, fields: Iterable[str]
) -> dict[str, dict]:
    """``{field: {"before": ..., "after": ...}}`` for each changed field (the
    list by name; the result carries the new list id alongside)."""
    return {
        f: {"before": getattr(before, f), "after": getattr(after, f)} for f in fields
    }


# --- alerts move with the due date (#232) ------------------------------------


@dataclass(frozen=True)
class Alert:
    """One EKAlarm, as the update plans it. ``at`` is local wall-clock time
    (absolute alerts); ``offset`` is seconds from the due date (relative)."""

    kind: str  # "absolute" | "relative" | "location"
    at: Optional[dt.datetime] = None
    offset: Optional[float] = None


def _due_at(due: str) -> dt.datetime:
    day, at = parse_due(due, NOT_UPDATED)
    return dt.datetime.combine(day, at or dt.time())


def _due_shift(old_due: str, new_due: str) -> dt.timedelta:
    """How far the due date moves: the full wall-clock difference when both
    have a time, else whole days (a date-only due date has no time to keep)."""
    (old_day, old_at), (new_day, new_at) = (
        parse_due(old_due, NOT_UPDATED),
        parse_due(new_due, NOT_UPDATED),
    )
    if old_at is not None and new_at is not None:
        return _due_at(new_due) - _due_at(old_due)
    return dt.timedelta(days=(new_day - old_day).days)


def _at_due(a: Alert, due_at: Optional[dt.datetime]) -> bool:
    """The alert Reminders' own time field makes: at the due time exactly."""
    if due_at is None:
        return False
    if a.kind == "absolute":
        return a.at.replace(second=0, microsecond=0) == due_at
    return a.kind == "relative" and a.offset == 0


def plan_alerts(
    alerts: Sequence[Alert], old_due: Optional[str], new_due: Optional[str]
) -> list[Optional[Alert]]:
    """Each alert after the due date moves from ``old_due`` to ``new_due``
    (None = cleared), aligned with ``alerts``; None marks one to remove.

    Re-dating shifts absolute alerts by the due date's move in wall-clock time
    (an alert at the due time stays at it, one a day before stays a day
    before); relative alerts already follow the due date, and location alerts
    don't depend on it. With no old due date there is nothing to shift from.
    Clearing drops the alert at the due time and refuses when another
    time-based alert would be left behind."""
    if new_due is not None:
        if old_due is None:
            return list(alerts)
        shift = _due_shift(old_due, new_due)
        return [
            replace(a, at=a.at + shift) if a.kind == "absolute" else a for a in alerts
        ]
    due_at = _due_at(old_due) if old_due and len(old_due) > 10 else None
    planned = [None if _at_due(a, due_at) else a for a in alerts]
    if any(a is not None and a.kind != "location" for a in planned):
        raise RemindersRefusal(
            "that reminder has another alert besides the one at its due time; "
            "clearing the due date would leave it firing on its own. Change it in "
            "the Reminders app; " + NOT_UPDATED
        )
    return planned


def describe_alert(a: Alert) -> str:
    if a.kind == "absolute":
        return a.at.strftime("%Y-%m-%dT%H:%M")
    if a.kind == "relative":
        return f"relative {int(a.offset)}s"
    return "location"
