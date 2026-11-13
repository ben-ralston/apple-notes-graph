"""Server-side guards for Reminders writes (issue #171, Phase 2). Pure: no EventKit."""

import datetime as dt

import pytest

from apple_notes_mcp.reminders import guards as g
from apple_notes_mcp.reminders.store import Reminder, ReminderList

TODAY = dt.date(2026, 10, 27)
GUARDED = ("Grocery List", "errands this week!")


def _r(
    rid="r1",
    title="SECRET-TITLE",
    list_id="a",
    list_name="Reminders",
    due="2026-10-27",
    recurring=False,
    completed=False,
    completed_at=None,
    notes="SECRET-NOTES",
    priority="none",
):
    return Reminder(
        id=rid,
        title=title,
        list_id=list_id,
        list=list_name,
        due=due,
        recurring=recurring,
        completed=completed,
        completed_at=completed_at,
        notes=notes,
        priority=priority,
    )


def _copy(**kw):
    base = dict(
        rid="c1", completed=True, completed_at="2026-10-27T09:00", due="2026-10-26"
    )
    base.update(kw)
    return _r(**base)


# --- completion ---------------------------------------------------------------


def test_blocklisted_list_is_refused_case_insensitively():
    with pytest.raises(g.RemindersRefusal) as e:
        g.check_completable(_r(list_name="grocery list"), "2026-10-27", GUARDED, None)
    assert str(e.value) == (
        'list "grocery list" is in reminders.never_complete_lists (remote.json); '
        "nothing was completed."
    )


def test_already_completed_is_refused():
    with pytest.raises(
        g.RemindersRefusal, match="already completed; nothing was completed"
    ):
        g.check_completable(_r(completed=True), "2026-10-27", GUARDED, None)


def test_expected_due_mismatch_shows_both_values():
    with pytest.raises(g.RemindersRefusal) as e:
        g.check_completable(_r(due="2026-10-28"), "2026-10-27", GUARDED, None)
    msg = str(e.value)
    assert "expected_due '2026-10-27'" in msg
    assert "current due '2026-10-28'" in msg
    assert "reminders_list" in msg and msg.endswith("nothing was completed.")


def test_undated_reminder_needs_expected_due_none():
    g.check_completable(_r(due=None), None, GUARDED, None)
    with pytest.raises(
        g.RemindersRefusal, match="expected_due '2026-10-27'.*current due none"
    ):
        g.check_completable(_r(due=None), "2026-10-27", GUARDED, None)
    with pytest.raises(
        g.RemindersRefusal, match="expected_due none.*current due '2026-10-27'"
    ):
        g.check_completable(_r(), None, GUARDED, None)


def test_matching_open_reminder_passes():
    g.check_completable(_r(due="2026-10-27T09:00"), "2026-10-27T09:00", GUARDED, None)


def test_recurring_with_completed_copy_today_is_refused():
    with pytest.raises(g.RemindersRefusal) as e:
        g.check_completable(
            _r(recurring=True, due="2026-10-28"), "2026-10-28", GUARDED, _copy()
        )
    msg = str(e.value)
    assert "already completed today (at 2026-10-27T09:00)" in msg
    assert "next occurrence (2026-10-28)" in msg
    assert msg.endswith("Nothing was completed.")


def test_completed_today_is_ignored_for_non_recurring():
    g.check_completable(_r(recurring=False), "2026-10-27", GUARDED, _copy())


def test_guards_run_in_design_order():
    # Fails guard 1 (blocklist) and guard 3 (expected_due): guard 1 wins.
    with pytest.raises(g.RemindersRefusal, match="never_complete_lists"):
        g.check_completable(_r(list_name="Grocery List"), "1999-01-01", GUARDED, None)
    # Fails guard 2 and 3: guard 2 wins.
    with pytest.raises(g.RemindersRefusal, match="already completed"):
        g.check_completable(_r(completed=True), "1999-01-01", GUARDED, None)
    # Fails guard 3 and 4: guard 3 wins.
    with pytest.raises(g.RemindersRefusal, match="expected_due"):
        g.check_completable(_r(recurring=True), "1999-01-01", GUARDED, _copy())


def test_completed_today_copy_matches_title_list_and_date_only():
    r = _r(recurring=True)
    hit = _copy()
    others = [
        _copy(rid="x1", title="Other"),
        _copy(rid="x2", list_id="b"),
        _copy(rid="x3", completed_at="2026-10-26T23:59"),
        _copy(rid="x4", completed=False, completed_at=None),
        _copy(rid="r1"),  # the reminder itself
    ]
    assert g.completed_today_copy(r, others, TODAY) is None
    assert g.completed_today_copy(r, [*others, hit], TODAY) is hit


# --- creation -----------------------------------------------------------------


def test_parse_due_formats():
    assert g.parse_due("2026-10-27") == (dt.date(2026, 10, 27), None)
    assert g.parse_due("2026-10-27T09:05") == (dt.date(2026, 10, 27), dt.time(9, 5))
    for bad in (
        "tomorrow",
        "2026-13-01",
        "2026-10-27 09:00",
        "2026-10-27T9",
        "2026-10-27T09:00:00",
    ):
        with pytest.raises(g.RemindersRefusal, match="due must look like 2026-10-27"):
            g.parse_due(bad)


def test_create_title_and_notes_caps():
    assert g.check_creatable("  Buy stamps  ", None) == "Buy stamps"
    with pytest.raises(g.RemindersRefusal, match="title is empty"):
        g.check_creatable("   ", None)
    with pytest.raises(g.RemindersRefusal, match="500 characters"):
        g.check_creatable("x" * (g.TITLE_MAX_CHARS + 1), None)
    g.check_creatable("x" * g.TITLE_MAX_CHARS, "n" * g.CREATE_NOTES_MAX_CHARS)
    with pytest.raises(g.RemindersRefusal, match="2000 characters"):
        g.check_creatable("ok", "n" * (g.CREATE_NOTES_MAX_CHARS + 1))


LISTS = [
    ReminderList(id="a", name="Reminders", source="iCloud"),
    ReminderList(id="b", name="Reminders", source="Exchange"),
    ReminderList(id="p", name="Professional", source="iCloud"),
    ReminderList(id="g", name="Grocery List", source="iCloud"),
]


def test_create_list_resolution_by_name_and_id():
    assert g.resolve_create_list("professional", LISTS, None).id == "p"
    assert g.resolve_create_list("b", LISTS, None).id == "b"
    with pytest.raises(g.RemindersRefusal) as e:
        g.resolve_create_list("Nope", LISTS, None)
    assert "Grocery List, Professional, Reminders" in str(e.value)
    assert str(e.value).endswith("nothing was created.")


def test_create_list_name_shared_by_two_lists_is_ambiguous():
    with pytest.raises(g.RemindersRefusal) as e:
        g.resolve_create_list("Reminders", LISTS, None)
    assert str(e.value) == (
        '2 lists are named "Reminders"; pass its id from reminders_lists. '
        "Nothing was created."
    )


def test_create_list_outside_create_lists_names_the_key():
    assert g.resolve_create_list("Professional", LISTS, ("professional",)).id == "p"
    with pytest.raises(g.RemindersRefusal) as e:
        g.resolve_create_list("Grocery List", LISTS, ("Professional",))
    assert str(e.value) == (
        'list "Grocery List" is not in reminders.create_lists (remote.json); '
        "nothing was created."
    )
    # An id is checked by its list's name too.
    with pytest.raises(g.RemindersRefusal, match="create_lists"):
        g.resolve_create_list("g", LISTS, ("Professional",))


def test_open_duplicate_title_is_refused():
    open_items = [_r(rid="r9", title="Buy Stamps", list_name="Professional")]
    g.check_no_open_duplicate("Mail letter", open_items)
    with pytest.raises(g.RemindersRefusal) as e:
        g.check_no_open_duplicate("buy stamps", open_items)
    assert str(e.value) == (
        'an open reminder with the same title is already in list "Professional" '
        "(id r9); nothing was created."
    )


def test_refusal_messages_never_contain_title_or_notes():
    cases = [
        lambda: g.check_completable(_r(list_name="Grocery List"), "x", GUARDED, None),
        lambda: g.check_completable(_r(completed=True), "x", GUARDED, None),
        lambda: g.check_completable(_r(), "x", GUARDED, None),
        lambda: g.check_completable(_r(recurring=True), "2026-10-27", GUARDED, _copy()),
        lambda: g.check_creatable("SECRET-TITLE" * 100, None),
        lambda: g.check_creatable("ok", "SECRET-NOTES" * 500),
        lambda: g.check_no_open_duplicate("secret-title", [_r()]),
    ]
    for case in cases:
        with pytest.raises(g.RemindersRefusal) as e:
            case()
        assert "SECRET" not in str(e.value).upper()


# --- multi-review round 1 (#171 Phase 2) ---------------------------------------


def test_list_names_match_after_strip_on_both_sides():
    # A Reminders list typed with a stray space is still blocklisted.
    with pytest.raises(g.RemindersRefusal, match="never_complete_lists"):
        g.check_completable(_r(list_name="Grocery List "), "2026-10-27", GUARDED, None)
    lists = [ReminderList(id="p", name="Professional ", source="iCloud")]
    assert g.resolve_create_list(" professional", lists, ("Professional",)).id == "p"


def test_duplicate_check_strips_existing_titles():
    with pytest.raises(g.RemindersRefusal, match="same title"):
        g.check_no_open_duplicate("buy milk", [_r(rid="r9", title="Buy milk ")])


def test_completed_today_copy_requires_the_same_time_of_day():
    evening = _r(recurring=True, due="2026-10-27T20:00")
    morning_copy = _copy(due="2026-10-27T08:00")
    own_copy = _copy(rid="c2", due="2026-10-26T20:00")
    assert g.completed_today_copy(evening, [morning_copy], TODAY) is None
    assert g.completed_today_copy(evening, [morning_copy, own_copy], TODAY) is own_copy
    # A date-only reminder matches only a date-only copy.
    dated = _r(recurring=True, due="2026-10-28")
    assert g.completed_today_copy(dated, [morning_copy], TODAY) is None
    assert g.completed_today_copy(dated, [_copy(due="2026-10-26")], TODAY) is not None


# --- updating (#232) ----------------------------------------------------------


def _edit(**kw):
    return g.ReminderEdit(**kw)


def test_edit_args_need_at_least_one_change():
    with pytest.raises(
        g.RemindersRefusal, match="nothing to change.*nothing was changed"
    ):
        g.check_edit_args(_edit())


def test_edit_args_strip_and_cap_the_title():
    assert g.check_edit_args(_edit(title="  New name ")).title == "New name"
    with pytest.raises(g.RemindersRefusal, match="title is empty; nothing was changed"):
        g.check_edit_args(_edit(title="   "))
    with pytest.raises(
        g.RemindersRefusal, match="title is over 500 characters; nothing was changed"
    ):
        g.check_edit_args(_edit(title="x" * 501))


def test_edit_args_check_due_notes_and_priority():
    with pytest.raises(
        g.RemindersRefusal, match="due must look like 2026-10-27.*nothing was changed"
    ):
        g.check_edit_args(_edit(due="Friday"))
    with pytest.raises(g.RemindersRefusal, match="due or clear_due, not both"):
        g.check_edit_args(_edit(due="2026-11-01", clear_due=True))
    with pytest.raises(g.RemindersRefusal, match="notes or clear_notes, not both"):
        g.check_edit_args(_edit(notes="n", clear_notes=True))
    with pytest.raises(g.RemindersRefusal, match="notes are empty; pass clear_notes"):
        g.check_edit_args(_edit(notes=""))
    with pytest.raises(
        g.RemindersRefusal, match="notes are over 2000 characters; nothing was changed"
    ):
        g.check_edit_args(_edit(notes="n" * 2001))
    with pytest.raises(
        g.RemindersRefusal, match="priority must be one of none, low, medium, high"
    ):
        g.check_edit_args(_edit(priority="urgent"))
    assert g.check_edit_args(_edit(priority="HIGH")).priority == "high"


def test_updatable_refuses_completed_and_stale_reads():
    with pytest.raises(
        g.RemindersRefusal, match="already completed; nothing was changed"
    ):
        g.check_updatable(_r(completed=True), "2026-10-27", None)
    with pytest.raises(
        g.RemindersRefusal,
        match="expected_due '2026-10-26' doesn't match the reminder's current due '2026-10-27'.*nothing was changed",
    ):
        g.check_updatable(_r(), "2026-10-26", None)
    with pytest.raises(
        g.RemindersRefusal, match="expected_title doesn't match.*nothing was changed"
    ):
        g.check_updatable(_r(title="Pay rent"), "2026-10-27", "Pay the rent")
    g.check_updatable(_r(title="Pay rent"), "2026-10-27", "Pay rent")
    g.check_updatable(_r(), "2026-10-27", None)


def test_changed_fields_lists_only_real_changes():
    r = _r(title="Pay rent", due="2026-10-27", notes=None, priority="none")
    edit = _edit(title="Pay rent", due="2026-10-30", priority="high")
    assert g.changed_fields(r, edit) == ("due", "priority")
    assert g.changed_fields(r, _edit(list_id="b")) == ("list",)
    assert g.changed_fields(r, _edit(clear_due=True, notes="call first")) == (
        "due",
        "notes",
    )
    assert g.changed_fields(_r(notes="old"), _edit(clear_notes=True)) == ("notes",)


def test_changed_fields_refuses_a_call_that_changes_nothing():
    r = _r(title="Pay rent", due=None, notes=None, list_id="a")
    for edit in (
        _edit(title="Pay rent"),
        _edit(clear_due=True),
        _edit(clear_notes=True),
        _edit(list_id="a"),
        _edit(priority="none"),
    ):
        with pytest.raises(
            g.RemindersRefusal, match="changes nothing; nothing was changed"
        ):
            g.changed_fields(r, edit)


def test_recurring_due_edits_are_refused_but_other_fields_pass():
    r = _r(recurring=True, due="2026-10-24")
    for edit in (_edit(due="2026-10-26"), _edit(clear_due=True)):
        with pytest.raises(
            g.RemindersRefusal, match="repeats.*whole series.*nothing was changed"
        ):
            g.changed_fields(r, edit)
    assert g.changed_fields(r, _edit(due="2026-10-24", title="Rent")) == ("title",)
    assert g.changed_fields(r, _edit(list_id="b")) == ("list",)


def test_update_target_list_respects_create_lists():
    lists = [
        ReminderList("a", "Reminders", "iCloud"),
        ReminderList("w", "Work", "iCloud"),
    ]
    assert g.resolve_create_list("work", lists, None, g.NOT_UPDATED).id == "w"
    with pytest.raises(
        g.RemindersRefusal,
        match='"Work" is not in reminders.create_lists.*nothing was changed',
    ):
        g.resolve_create_list("w", lists, ("Reminders",), g.NOT_UPDATED)


def test_duplicate_check_can_skip_the_reminder_itself():
    me = _r(rid="r1", title="Pay rent")
    g.check_no_open_duplicate("pay RENT", [me], g.NOT_UPDATED, ignore_id="r1")
    with pytest.raises(
        g.RemindersRefusal, match=r"same title.*\(id r2\); nothing was changed"
    ):
        g.check_no_open_duplicate(
            "pay rent",
            [me, _r(rid="r2", title="Pay rent")],
            g.NOT_UPDATED,
            ignore_id="r1",
        )


def test_update_refusals_never_contain_title_or_notes():
    r = _r(recurring=True)
    attempts = [
        lambda: g.check_updatable(_r(completed=True), "2026-10-27", None),
        lambda: g.check_updatable(r, "2026-10-26", "SECRET-OTHER"),
        lambda: g.check_updatable(r, "2026-10-27", "SECRET-OTHER"),
        lambda: g.changed_fields(r, _edit(due="2026-11-01")),
        lambda: g.changed_fields(r, _edit(title="SECRET-TITLE")),
        lambda: g.check_edit_args(_edit(notes="SECRET-NOTES" * 200)),
    ]
    for attempt in attempts:
        with pytest.raises(g.RemindersRefusal) as e:
            attempt()
        assert "SECRET" not in str(e.value)


def test_describe_changes_reports_before_and_after():
    before = _r(
        title="Old", due="2026-10-27", notes=None, list_id="a", list_name="Reminders"
    )
    after = _r(title="New", due=None, notes="n", list_id="w", list_name="Work")
    assert g.describe_changes(before, after, ("title", "due", "notes", "list")) == {
        "title": {"before": "Old", "after": "New"},
        "due": {"before": "2026-10-27", "after": None},
        "notes": {"before": None, "after": "n"},
        "list": {"before": "Reminders", "after": "Work"},
    }


# --- review round 1 (#232) ----------------------------------------------------


def test_move_out_of_a_never_complete_list_is_refused():
    r = _r(list_name="Grocery List")
    with pytest.raises(
        g.RemindersRefusal,
        match='"Grocery List" is in reminders.never_complete_lists.*moved out; nothing was changed',
    ):
        g.check_move(r, GUARDED, same_account=True)
    g.check_move(_r(list_name="Reminders"), GUARDED, same_account=True)


def test_move_to_another_account_is_refused():
    with pytest.raises(g.RemindersRefusal, match="another account.*nothing was changed"):
        g.check_move(_r(), GUARDED, same_account=False)


def test_rename_or_move_of_recurring_completed_today_is_refused():
    r = _r(recurring=True, due="2026-10-28")
    copy = _copy(title=r.title, list_id=r.list_id)
    with pytest.raises(g.RemindersRefusal, match="completed today.*nothing was changed"):
        g.check_completion_link(r, copy)
    g.check_completion_link(r, None)


def _abs(s):
    return g.Alert("absolute", at=dt.datetime.fromisoformat(s))


REL = g.Alert("relative", offset=-3600.0)
LOC = g.Alert("location")


def test_redate_shifts_absolute_alerts_by_the_due_move():
    alerts = [_abs("2026-11-03T09:00"), _abs("2026-11-02T09:00"), REL, LOC]
    assert g.plan_alerts(alerts, "2026-11-03T09:00", "2026-11-06T10:30") == [
        _abs("2026-11-06T10:30"),  # at the due time -> at the new due time
        _abs("2026-11-05T10:30"),  # 1 day before -> still 1 day before
        REL,  # relative: follows the due date as it is
        LOC,  # location: untouched
    ]


def test_redate_with_a_date_only_side_shifts_whole_days():
    alerts = [_abs("2026-11-03T08:00")]
    assert g.plan_alerts(alerts, "2026-11-03", "2026-11-05T14:00") == [_abs("2026-11-05T08:00")]
    assert g.plan_alerts(alerts, "2026-11-03T09:00", "2026-11-04") == [_abs("2026-11-04T08:00")]
    assert g.plan_alerts(alerts, "2026-11-03", "2026-11-01") == [_abs("2026-11-01T08:00")]


def test_adding_a_due_date_leaves_alerts_alone():
    alerts = [_abs("2026-11-03T08:00"), REL]
    assert g.plan_alerts(alerts, None, "2026-11-05T14:00") == alerts


def test_shift_is_wall_clock_across_dst():
    # US DST ends 2026-11-01: a 09:00 alert stays at 09:00 local after a 2-day move.
    assert g.plan_alerts([_abs("2026-10-31T09:00")], "2026-10-31T09:00", "2026-11-02T09:00") == [
        _abs("2026-11-02T09:00")
    ]


def test_clear_due_removes_the_alert_at_the_due_time_only():
    at_due = _abs("2026-11-03T09:00")
    zero = g.Alert("relative", offset=0.0)
    assert g.plan_alerts([at_due, LOC], "2026-11-03T09:00", None) == [None, LOC]
    assert g.plan_alerts([zero], "2026-11-03T09:00", None) == [None]
    for other in (_abs("2026-11-02T09:00"), REL):
        with pytest.raises(g.RemindersRefusal, match="other alert.*nothing was changed"):
            g.plan_alerts([at_due, other], "2026-11-03T09:00", None)
    with pytest.raises(g.RemindersRefusal, match="other alert"):
        g.plan_alerts([_abs("2026-11-03T09:00")], "2026-11-03", None)  # date-only: no at-due alert


def test_describe_alert():
    assert [g.describe_alert(a) for a in (_abs("2026-11-03T09:00"), REL, LOC)] == [
        "2026-11-03T09:00", "relative -3600s", "location",
    ]


def test_stale_due_refusal_mentions_an_earlier_call_may_have_applied():
    with pytest.raises(g.RemindersRefusal, match="or an earlier call already applied this edit"):
        g.check_updatable(_r(), "2026-10-26", None)
