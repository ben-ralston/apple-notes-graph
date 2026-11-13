"""Accept / apply / undo for queued remote writes (issue #169).

Everything Notes-facing is faked: a tiny in-memory "world" stands in for the
database, the content reader and the stdio write tools, so no GUI is needed.
"""

from types import SimpleNamespace

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from apple_notes.exceptions import (
    EditLockTimeoutError,
    EncryptedNoteError,
    NoteNotFoundError,
    NotesNotFrontmostError,
    NotesNotRespondingError,
    PartialWriteError,
    SessionLockedError,
    WriteVerificationError,
)
from apple_notes.sections import NoteBusyError
from apple_notes_mcp.remote.write_apply import WriteApplier, WriteRefused
from apple_notes_mcp.remote.write_queue import WriteQueue, WriteQueueConfig


class World:
    def __init__(self):
        self.notes = {}
        self.next_pk = 100

    def add(
        self, title, body="body line", checks=None, folder="Notes", encrypted=False
    ):
        self.next_pk += 1
        self.notes[self.next_pk] = SimpleNamespace(
            pk=self.next_pk,
            name=title,
            uuid=f"U-{self.next_pk}",
            folder=folder,
            version=1,
            text=f"{title}\n{body}",
            checks=dict(checks or {}),
            encrypted=encrypted,
            rendered_extra="",
            attachments=[],
        )
        return self.notes[self.next_pk]

    def touch(self, note, text=None):
        note.version += 1
        if text is not None:
            note.text = text


class FakeDB:
    def __init__(self, world):
        self.w = world

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def count_notes_titled(self, name):
        return sum(n.name.casefold() == name.casefold() for n in self.w.notes.values())

    def count_live_notes_titled(self, name):
        return sum(
            n.name.casefold() == name.casefold() and n.folder != "Recently Deleted"
            for n in self.w.notes.values()
        )

    def get_note_by_name(self, name):
        for n in self.w.notes.values():
            if n.name == name and n.folder != "Recently Deleted":
                return n
        raise NoteNotFoundError(name)

    def note_fingerprint(self, pk):
        n = self.w.notes.get(pk)
        return None if n is None else f"fp-{pk}-{n.version}"

    def note_media_bytes(self, pk):
        return getattr(self.w, "media", 0)


class FakeReader:
    def __init__(self, world):
        self.w = world

    def get_note_content_for_note(self, note):
        if note.encrypted:
            raise EncryptedNoteError("locked")
        return SimpleNamespace(
            resolved_text="# " + note.text + note.rendered_extra, raw_text=note.text,
            attachments=list(note.attachments),
        )


def checklist_state(world):
    def state(db, pk, item_text):
        for pos, (text, checked) in world.notes[pk].checks.items():
            if item_text in text:
                return pos, checked
        return None

    return state


def chained(exc):
    """A ToolError raised inside an except, as the stdio tools do."""
    try:
        raise exc
    except Exception as e:
        try:
            raise ToolError(str(e))
        except ToolError as te:
            return te


class FakeStdio:
    def __init__(self, world):
        self.w = world
        self.calls = []
        self.raise_next = []
        self.toggle_effect = True

    def _by_name(self, name):
        return next(n for n in self.w.notes.values() if n.name == name)

    def _maybe_raise(self):
        if self.raise_next:
            raise self.raise_next.pop(0)

    def add_note(self, name, body, folder="Notes"):
        self.calls.append(("add_note", name))
        self._maybe_raise()
        self.w.add(name, body)
        return {"success": True}

    def update_note_content(self, name, body, operation, old_text=None):
        self.calls.append(("update_note_content", operation, name))
        self._maybe_raise()
        n = self._by_name(name)
        if operation == "append":
            self.w.touch(n, n.text + "\n" + body)
        elif operation == "replace":
            self.w.touch(n, f"{n.name}\n{body}")
        else:
            self.w.touch(n, n.text.replace(old_text, body, 1))
        return {"success": True}

    def toggle_checklist_item(self, name, item_text):
        self.calls.append(("toggle_checklist_item", name))
        self._maybe_raise()
        n = self._by_name(name)
        if self.toggle_effect:
            for pos, (text, checked) in n.checks.items():
                if item_text in text:
                    n.checks[pos] = (text, not checked)
                    break
        self.w.touch(n)
        return {"success": True}


@pytest.fixture
def world():
    return World()


@pytest.fixture
def env(tmp_path, world):
    queue = WriteQueue(WriteQueueConfig(state_dir=tmp_path / "wq"))
    stdio = FakeStdio(world)
    deleted = []
    applier = WriteApplier(
        queue,
        db_factory=lambda: FakeDB(world),
        reader_factory=lambda: FakeReader(world),
        checklist_state=checklist_state(world),
        stdio=stdio,
        delete_note=lambda name: deleted.append(name),
        sleep=lambda s: None,
    )
    return SimpleNamespace(queue=queue, stdio=stdio, applier=applier, deleted=deleted)


def append_args(name="Daily", body="more"):
    return {"name": name, "body": body, "operation": "append"}


# ------------------------------------------------------------------ accept


def test_accept_records_uuid_fingerprint_and_subject(env, world):
    note = world.add("Daily")
    out = env.applier.accept("update_note_content", append_args(), subject="owner")
    rec = env.queue.get(out["write_id"])
    assert rec["status"] == "queued" and rec["subject"] == "owner"
    assert (
        rec["note_uuid"] == note.uuid and rec["base_fingerprint"] == f"fp-{note.pk}-1"
    )
    assert out["queued_ahead"] == 0


@pytest.mark.parametrize(
    "setup, args, match",
    [
        (["Daily"], {"name": "daily", "body": "b"}, "already exists"),
        ([], {"name": "New", "body": "b", "folder": "Work"}, "Only 'Notes' folder"),
        ([], {"name": " ", "body": "b"}, "cannot be empty"),
        ([], {"name": "a\nb", "body": "b"}, "newlines"),
    ],
)
def test_accept_add_note_refusals(env, world, setup, args, match):
    for t in setup:
        world.add(t)
    with pytest.raises(WriteRefused, match=match):
        env.applier.accept("add_note", args)


def test_accept_refuses_missing_duplicate_case_mismatch_and_encrypted(env, world):
    with pytest.raises(WriteRefused, match="not found"):
        env.applier.accept("update_note_content", append_args())
    world.add("Daily")
    trashed = world.add("DAILY", folder="Recently Deleted")
    # One live note beside a trashed copy is not ambiguous (#83): the stdio
    # tool the apply runs through addresses the live note by id.
    assert env.applier.accept("update_note_content", append_args())["write_id"]
    trashed.folder = "Work"  # now two live notes share the title
    with pytest.raises(WriteRefused, match="2 notes"):
        env.applier.accept("update_note_content", append_args())
    world.add("Solo")
    with pytest.raises(WriteRefused, match="exact title"):
        env.applier.accept("update_note_content", append_args(name="solo"))
    world.add("Locked", encrypted=True)
    with pytest.raises(WriteRefused, match="encrypted"):
        env.applier.accept("update_note_content", append_args(name="Locked"))


def test_accept_validates_operation_and_old_text(env, world):
    world.add("Daily")
    with pytest.raises(WriteRefused, match="Invalid operation"):
        env.applier.accept(
            "update_note_content", {"name": "Daily", "body": "b", "operation": "delete"}
        )
    with pytest.raises(WriteRefused, match="old_text is required"):
        env.applier.accept(
            "update_note_content",
            {"name": "Daily", "body": "b", "operation": "find_replace"},
        )
    with pytest.raises(WriteRefused, match="only valid for find_replace"):
        env.applier.accept("update_note_content", {**append_args(), "old_text": "x"})


def test_accept_find_replace_runs_the_stdio_guards(env, world):
    note = world.add("Daily", body="alpha beta alpha")
    note.rendered_extra = " [[Linked]]"  # rendered markup absent from the raw note data
    fr = {"name": "Daily", "body": "z", "operation": "find_replace"}
    with pytest.raises(WriteRefused, match="2 times"):
        env.applier.accept("update_note_content", {**fr, "old_text": "alpha"})
    with pytest.raises(WriteRefused, match="not found in note body"):
        env.applier.accept("update_note_content", {**fr, "old_text": "gamma"})
    with pytest.raises(WriteRefused, match="underlying note data"):
        env.applier.accept(
            "update_note_content", {**fr, "old_text": "[[Linked]]"}
        )
    assert env.applier.accept("update_note_content", {**fr, "old_text": "beta"})[
        "write_id"
    ]


def test_accept_toggle_stores_desired_state(env, world):
    world.add("Todo", checks={7: ("buy milk", False)})
    with pytest.raises(WriteRefused, match="checklist item"):
        env.applier.accept(
            "toggle_checklist_item", {"name": "Todo", "item_text": "eggs"}
        )
    wid = env.applier.accept(
        "toggle_checklist_item", {"name": "Todo", "item_text": "milk"}
    )["write_id"]
    rec = env.queue.get(wid)
    assert rec["desired_checked"] is True and rec["item_position"] == 7


def test_accept_caps_become_refusals_with_the_cap_name(env, world):
    world.add("Daily")
    with pytest.raises(WriteRefused) as e:
        env.applier.accept("update_note_content", append_args(body="x" * 20_001))
    assert e.value.cap == "max_body_chars"


ONE_COL = "intro\n\n| a |\n|---|\n| 1 |\n| 2 |"
TWO_COL = "intro\n\n| a | b |\n|---|---|\n| 1 | 2 |"
THREE_COL = "intro\n\n| a | b | c |\n|---|---|---|\n| 1 | 2 | 3 |"


def _table_write(tool, body, operation=None):
    if tool == "add_note":
        return {"name": "Fresh", "body": body}
    args = {"name": "Daily", "body": body, "operation": operation}
    if operation == "find_replace":
        args["old_text"] = "body line"
    return args


TABLE_WRITES = [
    ("add_note", None),
    ("update_note_content", "append"),
    ("update_note_content", "replace"),
    ("update_note_content", "find_replace"),
]


@pytest.mark.parametrize("tool, operation", TABLE_WRITES)
@pytest.mark.parametrize(
    "body, match",
    [
        (ONE_COL, "one-column table.*Nothing was written"),
        (THREE_COL, "table with 3 columns.*Nothing was written"),
    ],
)
def test_accept_refuses_an_untypeable_table_and_queues_nothing(
    env, world, tool, operation, body, match
):
    """#264: the apply-time "Nothing was written" refusal (#122, #260) is
    returned at accept, before anything is queued."""
    world.add("Daily")
    with pytest.raises(WriteRefused, match=match):
        env.applier.accept(tool, _table_write(tool, body, operation))
    assert env.queue.list() == []


@pytest.mark.parametrize("tool, operation", TABLE_WRITES)
def test_accept_queues_and_applies_a_two_column_table(env, world, tool, operation):
    world.add("Daily")
    wid = env.applier.accept(tool, _table_write(tool, TWO_COL, operation))["write_id"]
    assert env.queue.get(wid)["status"] == "queued"
    assert env.applier.apply(wid) == "applied"
    assert env.stdio.calls[-1][0] == tool


# ------------------------------------------------------------------ apply


def _accept(env, tool="update_note_content", args=None):
    return env.applier.accept(tool, args or append_args())["write_id"]


def test_apply_append_journals_and_fingerprints(env, world):
    note = world.add("Daily")
    wid = _accept(env)
    world.touch(note)  # the owner edited it meanwhile: append still applies
    assert env.applier.apply(wid) == "applied"
    rec = env.queue.get(wid)
    assert rec["base_changed"] is True
    assert rec["prior_markdown"] == "# Daily\nbody line"
    assert rec["post_fingerprint"] == f"fp-{note.pk}-3"
    assert env.stdio.calls == [("update_note_content", "append", "Daily")]


def test_apply_add_note_fills_uuid_and_conflicts_if_title_appeared(env, world):
    wid = _accept(env, "add_note", {"name": "Fresh", "body": "b"})
    assert env.applier.apply(wid) == "applied"
    rec = env.queue.get(wid)
    assert rec["note_uuid"] and rec["post_fingerprint"]
    wid2 = _accept(env, "add_note", {"name": "Second", "body": "b"})
    world.add("second")
    assert env.applier.apply(wid2) == "conflict"
    assert env.stdio.calls == [("add_note", "Fresh")]


def test_apply_replace_conflicts_when_note_changed(env, world):
    note = world.add("Daily")
    wid = _accept(env, args={"name": "Daily", "body": "new", "operation": "replace"})
    world.touch(note)
    assert env.applier.apply(wid) == "conflict"
    assert "changed since" in env.queue.get(wid)["reason"]
    assert env.stdio.calls == []


def test_apply_find_replace_conflicts_when_match_no_longer_unique(env, world):
    note = world.add("Daily", body="token here")
    wid = _accept(
        env,
        args={
            "name": "Daily",
            "body": "x",
            "operation": "find_replace",
            "old_text": "token",
        },
    )
    world.touch(note, note.text + " token")
    assert env.applier.apply(wid) == "conflict"


def test_apply_conflicts_on_uuid_change_or_new_duplicate(env, world):
    note = world.add("Daily")
    wid = _accept(env)
    del world.notes[note.pk]
    world.add("Daily")  # same title, different note
    assert env.applier.apply(wid) == "conflict"
    wid2 = _accept(env)
    world.add("daily", folder="Work")  # a second live note
    assert env.applier.apply(wid2) == "conflict"


def test_apply_toggle_is_idempotent_and_confirms_the_item(env, world):
    note = world.add("Todo", checks={7: ("buy milk", False)})
    wid = _accept(env, "toggle_checklist_item", {"name": "Todo", "item_text": "milk"})
    note.checks[7] = ("buy milk", True)  # the owner ticked it by hand
    assert env.applier.apply(wid) == "applied"
    assert "already" in env.queue.get(wid)["reason"]
    assert env.stdio.calls == []

    note.checks[7] = ("buy milk", False)
    wid2 = _accept(env, "toggle_checklist_item", {"name": "Todo", "item_text": "milk"})
    env.stdio.toggle_effect = False  # the UI toggled something else
    assert env.applier.apply(wid2) == "failed"
    assert "different item" in env.queue.get(wid2)["reason"]


@pytest.mark.parametrize(
    "exc", [SessionLockedError("locked"), EditLockTimeoutError("busy")]
)
def test_apply_requeues_without_counting_an_attempt(env, world, exc):
    world.add("Daily")
    wid = _accept(env)
    env.stdio.raise_next = [chained(exc)]
    assert env.applier.apply(wid) == "queued"
    assert env.queue.get(wid)["attempts"] == 0


@pytest.mark.parametrize("exc_type", [NotesNotFrontmostError, NotesNotRespondingError])
def test_apply_retries_focus_failures_once_when_note_unchanged(env, world, exc_type):
    world.add("Daily")
    wid = _accept(env)
    env.stdio.raise_next = [chained(exc_type("x")), chained(exc_type("x"))]
    assert env.applier.apply(wid) == "queued"
    assert env.queue.get(wid)["attempts"] == 1
    assert env.applier.apply(wid) == "failed"


def test_apply_never_retries_a_possibly_partial_write(env, world):
    note = world.add("Daily")
    wid = _accept(env)

    def partial(*a, **k):
        world.touch(note)  # keystrokes landed before focus was lost
        raise chained(NotesNotFrontmostError("lost focus"))

    env.stdio.update_note_content = partial
    # #200: it landed in part - one undoable, never-retried status.
    assert env.applier.apply(wid) == "applied_unverified"
    rec = env.queue.get(wid)
    assert "changed during a failed attempt" in rec["reason"]
    assert rec["post_fingerprint"] is not None


def test_apply_other_errors_fail_and_never_stay_applying(env, world):
    world.add("Daily")
    wid = _accept(env)
    env.stdio.raise_next = [chained(WriteVerificationError("did not take"))]
    assert env.applier.apply(wid) == "failed"
    wid2 = _accept(env)
    env.stdio.raise_next = [RuntimeError("boom")]
    assert env.applier.apply(wid2) == "failed"
    assert env.queue.get(wid2)["status"] == "failed"


def test_apply_skips_records_it_cannot_claim(env, world):
    world.add("Daily")
    wid = _accept(env)
    env.queue.claim(wid)
    assert env.applier.apply(wid) == "applying"
    assert env.stdio.calls == []


def test_apply_post_fingerprint_unknown_when_db_never_changes(env, world):
    world.add("Daily")
    wid = _accept(env)
    env.stdio.update_note_content = lambda *a, **k: {
        "success": True
    }  # no visible change
    assert env.applier.apply(wid) == "applied"
    assert env.queue.get(wid)["post_fingerprint"] is None


# ------------------------------------------------------------------ undo


def test_undo_replace_restores_prior_body(env, world):
    note = world.add("Daily", body="original")
    wid = _accept(env, args={"name": "Daily", "body": "new", "operation": "replace"})
    env.applier.apply(wid)
    assert env.applier.undo(wid) == "undone"
    assert env.stdio.calls[-1] == ("update_note_content", "replace", "Daily")
    assert note.text == "Daily\noriginal"
    assert env.queue.get(wid)["status"] == "undone"


def test_undo_add_note_deletes_and_toggle_toggles_back(env, world):
    wid = _accept(env, "add_note", {"name": "Fresh", "body": "b"})
    env.applier.apply(wid)
    env.applier.undo(wid)
    assert env.deleted == ["Fresh"]
    note = world.add("Todo", checks={7: ("milk", False)})
    wid2 = _accept(env, "toggle_checklist_item", {"name": "Todo", "item_text": "milk"})
    env.applier.apply(wid2)
    env.applier.undo(wid2)
    assert note.checks[7] == ("milk", False)


def test_undo_refusals(env, world):
    note = world.add("Daily")
    wid = _accept(env)
    with pytest.raises(WriteRefused, match="only applied"):
        env.applier.undo(wid)
    env.applier.apply(wid)
    world.touch(note)
    with pytest.raises(WriteRefused, match="changed after"):
        env.applier.undo(wid)
    wid2 = _accept(env)
    env.stdio.update_note_content = lambda *a, **k: {"success": True}
    env.applier.apply(wid2)
    with pytest.raises(WriteRefused, match="post-write state unknown"):
        env.applier.undo(wid2)


# ------------------------------------------------------------------ review round 1


def test_undo_of_a_noop_toggle_leaves_the_checklist_alone(env, world):
    note = world.add("Todo", checks={7: ("milk", False)})
    wid = _accept(env, "toggle_checklist_item", {"name": "Todo", "item_text": "milk"})
    note.checks[7] = ("milk", True)  # the owner ticked it by hand: the apply is a no-op
    env.applier.apply(wid)
    assert env.applier.undo(wid) == "undone"
    assert note.checks[7] == ("milk", True)
    assert env.stdio.calls == []


def test_markdown_undo_refused_when_the_note_had_attachments(env, world):
    note = world.add("Photo note", body="caption")
    note.attachments = [SimpleNamespace(type_uti="public.jpeg")]
    wid = _accept(env, args={"name": "Photo note", "body": "x", "operation": "append"})
    env.applier.apply(wid)
    with pytest.raises(WriteRefused, match="attachments"):
        env.applier.undo(wid)
    assert env.stdio.calls == [("update_note_content", "append", "Photo note")]


def test_toggle_with_unconfirmed_post_state_is_applied_not_failed(env, world):
    world.add("Todo", checks={7: ("milk", False)})
    wid = _accept(env, "toggle_checklist_item", {"name": "Todo", "item_text": "milk"})
    env.stdio.toggle_checklist_item = lambda **k: {"success": True}  # Notes hasn't flushed yet
    assert env.applier.apply(wid) == "applied"
    rec = env.queue.get(wid)
    assert rec["post_fingerprint"] is None and "not confirmed" in rec["reason"]


def test_partial_write_seen_only_after_a_lazy_flush_is_not_retried(env, world):
    note = world.add("Daily")
    wid = _accept(env)
    env.stdio.raise_next = [chained(NotesNotRespondingError("timed out"))]
    flushed = []

    def sleep(_):
        if not flushed:
            flushed.append(1)
            world.touch(note)  # the paste landed; Notes persists it a moment later

    env.applier._sleep = sleep
    assert env.applier.apply(wid) == "applied_unverified"
    rec = env.queue.get(wid)
    assert "changed during a failed attempt" in rec["reason"]
    assert rec["post_fingerprint"] is not None


def test_undo_of_a_purged_toggle_is_refused_not_a_crash(env, world):
    world.add("Todo", checks={7: ("milk", False)})
    wid = _accept(env, "toggle_checklist_item", {"name": "Todo", "item_text": "milk"})
    env.applier.apply(wid)
    env.queue.update(wid, args_json=None)
    with pytest.raises(WriteRefused, match="purged"):
        env.applier.undo(wid)


def test_cheap_caps_are_checked_before_touching_the_database(tmp_path):
    queue = WriteQueue(
        WriteQueueConfig(state_dir=tmp_path / "wq", allowed_operations=frozenset({"append"}))
    )

    def no_db():
        raise AssertionError("resolved the note before checking the caps")

    applier = WriteApplier(queue, db_factory=no_db)
    with pytest.raises(WriteRefused) as e:
        applier.accept("update_note_content", {"name": "D", "body": "b", "operation": "replace"})
    assert e.value.cap == "allowed_operations"
    with pytest.raises(WriteRefused) as e:
        applier.accept("update_note_content", append_args(name="D", body="x" * 20_001))
    assert e.value.cap == "max_body_chars"


# ------------------------------------------------------------------ review round 2


def test_tags_and_note_links_do_not_block_markdown_undo(env, world):
    note = world.add("Tagged", body="text")
    note.attachments = [
        SimpleNamespace(type_uti="com.apple.notes.inlinetextattachment.hashtag"),
        SimpleNamespace(type_uti="com.apple.notes.inlinetextattachment.link"),
    ]
    wid = _accept(env, args={"name": "Tagged", "body": "x", "operation": "append"})
    env.applier.apply(wid)
    assert env.applier.undo(wid) == "undone"


def test_note_missing_while_confirming_is_applied_unconfirmed_not_failed(env, world):
    note = world.add("Daily")
    wid = _accept(env)

    def renamed(*a, **k):
        note.name = "Daily (renamed)"  # the write landed, but the title moved
        world.touch(note)
        return {"success": True}

    env.stdio.update_note_content = renamed
    assert env.applier.apply(wid) == "applied"
    rec = env.queue.get(wid)
    assert rec["post_fingerprint"] is None and "not confirmed" in rec["reason"]


def test_toggle_confirmation_errors_mean_unconfirmed_not_failed(env, world):
    world.add("Todo", checks={7: ("milk", False)})
    wid = _accept(env, "toggle_checklist_item", {"name": "Todo", "item_text": "milk"})
    real_state = env.applier._checklist_state
    calls = []

    def flaky(db, pk, item):
        calls.append(1)
        if len(calls) > 1:  # the pre-apply check succeeds; the post-check blows up
            raise EncryptedNoteError("locked meanwhile")
        return real_state(db, pk, item)

    env.applier._checklist_state = flaky
    assert env.applier.apply(wid) == "applied"
    assert "not confirmed" in env.queue.get(wid)["reason"]


# ------------------------------------------------------------------ review round 3


@pytest.mark.parametrize("tool", ["update_note_content", "toggle_checklist_item"])
def test_note_locked_after_queueing_is_a_conflict_not_an_internal_error(env, world, tool):
    note = world.add("Todo", checks={7: ("milk", False)})
    args = (
        {"name": "Todo", "item_text": "milk"}
        if tool == "toggle_checklist_item"
        else {"name": "Todo", "body": "b", "operation": "append"}
    )
    wid = _accept(env, tool, args)
    note.encrypted = True  # the owner locked the note meanwhile

    def locked_state(db, pk, item):
        raise EncryptedNoteError("locked")

    env.applier._checklist_state = locked_state
    assert env.applier.apply(wid) == "conflict"
    assert "locked" in env.queue.get(wid)["reason"]
    assert env.stdio.calls == []


# ------------------------------------------------------- replace_note_link (#183)

HOME_U = "AAAAAAAA-0000-0000-0000-000000000001"
PLANS_U = "AAAAAAAA-0000-0000-0000-000000000002"


class LinkDB(FakeDB):
    """FakeDB plus the reads relink.plan_relink needs."""

    def get_note_link_targets(self, pk):
        return list(getattr(self.w.notes[pk], "links", []))

    def _live(self):
        return [n for n in self.w.notes.values() if n.folder != "Recently Deleted"]

    def get_all_notes(self):
        return self._live()

    def build_uuid_lookup(self):
        out = {}
        for n in self._live():
            out[n.uuid.upper()] = n
            out[n.uuid.lower()] = n
        return out


class LinkStdio(FakeStdio):
    def replace_note_link(self, name, old_target, new_target, replace_all=False):
        self.calls.append(("replace_note_link", name, old_target, new_target, replace_all))
        self._maybe_raise()
        n = self._by_name(name)
        n.links = [new_target if t == old_target else t for t in n.links]
        self.w.touch(n)
        return {"success": True}


@pytest.fixture
def lenv(tmp_path, world):
    queue = WriteQueue(WriteQueueConfig(state_dir=tmp_path / "wq"))
    stdio = LinkStdio(world)
    applier = WriteApplier(
        queue,
        db_factory=lambda: LinkDB(world),
        reader_factory=lambda: FakeReader(world),
        checklist_state=checklist_state(world),
        stdio=stdio,
        sleep=lambda s: None,
    )
    return SimpleNamespace(queue=queue, stdio=stdio, applier=applier)


def _link_world(world, links):
    home = world.add("★ Index")
    home.uuid = HOME_U
    plans = world.add("★ Reading List")
    plans.uuid = PLANS_U
    daily = world.add("Oct 28 Journal:")
    daily.links = list(links)
    return daily


def relink_args(**over):
    args = {"name": "Oct 28 Journal:", "old_target": "★ Index", "new_target": "★ Reading List"}
    args.update(over)
    return args


def test_accept_relink_stores_resolved_uuids(lenv, world):
    _link_world(world, [HOME_U])
    out = lenv.applier.accept("replace_note_link", relink_args())
    rec = lenv.queue.get(out["write_id"])
    assert rec["operation"] == "replace_note_link"
    assert rec["args"]["old_target"] == HOME_U
    assert rec["args"]["new_target"] == PLANS_U


def test_accept_relink_refusal(lenv, world):
    _link_world(world, [PLANS_U])
    with pytest.raises(WriteRefused, match="No link to '★ Index'"):
        lenv.applier.accept("replace_note_link", relink_args())
    with pytest.raises(WriteRefused, match="old_target"):
        lenv.applier.accept("replace_note_link", relink_args(old_target=" "))


def test_apply_relink_calls_stdio_with_uuids(lenv, world):
    daily = _link_world(world, [HOME_U])
    wid = lenv.applier.accept("replace_note_link", relink_args())["write_id"]
    assert lenv.applier.apply(wid) == "applied"
    assert lenv.stdio.calls[-1] == ("replace_note_link", "Oct 28 Journal:", HOME_U, PLANS_U, False)
    assert daily.links == [PLANS_U]


def test_apply_relink_conflict_when_link_gone(lenv, world):
    daily = _link_world(world, [HOME_U])
    wid = lenv.applier.accept("replace_note_link", relink_args())["write_id"]
    daily.links = []
    world.touch(daily)
    assert lenv.applier.apply(wid) == "conflict"
    assert "no longer matches" in lenv.queue.get(wid)["reason"]
    assert not any(c[0] == "replace_note_link" for c in lenv.stdio.calls)


def test_undo_relink_reverses(lenv, world):
    daily = _link_world(world, [HOME_U])
    wid = lenv.applier.accept("replace_note_link", relink_args())["write_id"]
    lenv.applier.apply(wid)
    assert lenv.applier.undo(wid) == "undone"
    assert lenv.stdio.calls[-1] == ("replace_note_link", "Oct 28 Journal:", PLANS_U, HOME_U, True)
    assert daily.links == [HOME_U]


def test_undo_refused_when_note_already_linked_new_target(lenv, world):
    _link_world(world, [HOME_U, PLANS_U])
    wid = lenv.applier.accept("replace_note_link", relink_args())["write_id"]
    lenv.applier.apply(wid)
    assert lenv.queue.get(wid)["relink_prior_new_count"] == 1
    with pytest.raises(WriteRefused, match="already linked"):
        lenv.applier.undo(wid)


def test_queue_migrates_relink_column(tmp_path):
    import sqlite3

    state = tmp_path / "wq"
    WriteQueue(WriteQueueConfig(state_dir=state))
    conn = sqlite3.connect(state / "queue.sqlite")
    conn.execute("ALTER TABLE writes DROP COLUMN relink_prior_new_count")
    conn.commit()
    conn.close()
    WriteQueue(WriteQueueConfig(state_dir=state))  # re-open migrates
    conn = sqlite3.connect(state / "queue.sqlite")
    cols = {row[1] for row in conn.execute("PRAGMA table_info(writes)")}
    conn.close()
    assert "relink_prior_new_count" in cols


def test_undo_relink_failure_is_refused_not_crash(lenv, world):
    _link_world(world, [HOME_U])
    wid = lenv.applier.accept("replace_note_link", relink_args())["write_id"]
    lenv.applier.apply(wid)
    lenv.stdio.raise_next.append(ToolError("New target note X not found"))
    with pytest.raises(WriteRefused, match="undo failed: New target note X not found"):
        lenv.applier.undo(wid)


# ------------------------------------------------------- applied_unverified (#200)


def _partial(world, note, exc=None):
    def write(*a, **k):
        world.touch(note)  # the paste landed...
        raise chained(exc or PartialWriteError("1 of 1 note link(s) did not land: [[X]]"))

    return write


def test_apply_partial_write_is_applied_unverified(env, world):
    note = world.add("Daily")
    wid = _accept(env)
    calls = []
    write = _partial(world, note)
    env.stdio.update_note_content = lambda *a, **k: (calls.append(1), write())
    assert env.applier.apply(wid) == "applied_unverified"
    rec = env.queue.get(wid)
    assert "did not land" in rec["reason"]
    assert rec["post_fingerprint"] is not None and rec["applied_at"] is not None
    assert env.applier.apply(wid) == "applied_unverified"  # never re-run
    assert calls == [1]


def test_plain_verification_error_after_link_reclassify_is_failed(env, world):
    """ui_operations raises WriteVerificationError FROM the link error when
    nothing changed: only the outermost verification error decides."""
    world.add("Daily")
    wid = _accept(env)
    try:
        try:
            raise PartialWriteError("link did not land")
        except PartialWriteError as e:
            raise WriteVerificationError("unchanged afterward") from e
    except WriteVerificationError as wve:
        exc = wve
    env.stdio.raise_next = [chained(exc)]
    assert env.applier.apply(wid) == "failed"


def test_undo_applied_unverified_replace_restores(env, world):
    note = world.add("Daily", body="original")
    wid = _accept(env, args={"name": "Daily", "body": "new", "operation": "replace"})

    def write(name, body, operation, old_text=None):
        world.touch(note, f"{note.name}\n{body}")
        raise chained(PartialWriteError("checkbox 1 is unchecked, expected checked"))

    env.stdio.update_note_content = write
    assert env.applier.apply(wid) == "applied_unverified"
    del env.stdio.update_note_content  # undo goes through the real fake
    assert env.applier.undo(wid) == "undone"
    assert note.text == "Daily\noriginal"


def test_apply_partial_add_note_unverified_and_undo_deletes(env, world):
    wid = _accept(env, "add_note", {"name": "Fresh", "body": "b"})

    def create(name, body, folder="Notes"):
        world.add(name, body)
        raise chained(PartialWriteError("[[X]] did not land"))

    env.stdio.add_note = create
    assert env.applier.apply(wid) == "applied_unverified"
    rec = env.queue.get(wid)
    assert rec["note_uuid"] and rec["post_fingerprint"]
    assert env.applier.undo(wid) == "undone"
    assert env.deleted == ["Fresh"]


def test_add_note_focus_loss_after_create_is_applied_unverified_and_undoable(
    env, world
):
    wid = _accept(env, "add_note", {"name": "Fresh", "body": "b"})

    def create(name, body, folder="Notes"):
        world.add(name, body)
        raise chained(NotesNotFrontmostError("lost focus"))

    env.stdio.add_note = create
    assert env.applier.apply(wid) == "applied_unverified"
    assert env.applier.undo(wid) == "undone"
    assert env.deleted == ["Fresh"]


def test_undo_unverified_toggle_refused_when_the_toggle_never_landed(env, world):
    """Review round 1: a focus loss whose 'change' was the owner's own typing ends
    applied_unverified; undo must not flip an item that was never toggled."""
    note = world.add("Todo", checks={7: ("milk", False)})
    wid = _accept(env, "toggle_checklist_item", {"name": "Todo", "item_text": "milk"})

    def lost(*a, **k):
        world.touch(note)  # the owner typed; the toggle itself never landed
        raise chained(NotesNotFrontmostError("lost focus"))

    env.stdio.toggle_checklist_item = lost
    assert env.applier.apply(wid) == "applied_unverified"
    del env.stdio.toggle_checklist_item
    with pytest.raises(WriteRefused, match="by hand"):
        env.applier.undo(wid)
    assert note.checks[7] == ("milk", False)


# ------------------------------------------------------- edit_note_section (#184)

from apple_notes import sections as _sections  # noqa: E402
from apple_notes_mcp.remote import write_apply as _wa  # noqa: E402


def _fake_load_snapshot(db, pk, render=None):
    note = db.w.notes[pk]
    lines = note.text.split("\n")
    paras = []
    start = 0
    for i, line in enumerate(lines):
        piece = line + ("\n" if i < len(lines) - 1 else "")
        kind = "title" if i == 0 else ("heading" if line.startswith("# ") else "body")
        atts = tuple(getattr(note, "para_atts", {}).get(i, ()))
        paras.append(
            _sections.Paragraph(i, start, piece, kind, 0, None, atts, ())
        )
        start += len(piece)
    rendered = tuple(render(pk)) if render else (None,) * len(paras)
    return _sections.NoteSnapshot(pk, tuple(paras), rendered)


class SectionReader(FakeReader):
    def paragraph_renderer(self, note):
        def render(pk):
            tables = getattr(note, "table_paras", set())
            return [
                None if i in tables else line
                for i, line in enumerate(note.text.split("\n"))
            ]

        return render


class SectionStdio(FakeStdio):
    def edit_note_section(
        self, name, operation, heading=None, section_text=None, body=None,
        old_text=None, new_text=None,
    ):
        self.calls.append(
            ("edit_note_section", name, operation, heading, section_text, body)
        )
        self._maybe_raise()
        n = self._by_name(name)
        snap = _fake_load_snapshot(FakeDB(self.w), n.pk, SectionReader(self.w).paragraph_renderer(n))
        sec = _sections.resolve_section(snap, name, heading=heading, section_text=section_text)
        lines = n.text.split("\n")
        if operation == "replace":
            lines[sec.a : sec.b] = body.split("\n")
        elif operation == "append":
            lines[sec.b : sec.b] = body.split("\n")
        elif operation == "delete":
            del lines[sec.a : sec.b]
        else:
            lines[sec.a : sec.b] = [
                l.replace(old_text, new_text) for l in lines[sec.a : sec.b]
            ]
        self.w.touch(n, "\n".join(lines))
        return {"success": True}


@pytest.fixture
def senv(tmp_path, world, monkeypatch):
    monkeypatch.setattr(_wa, "load_snapshot", _fake_load_snapshot)
    queue = WriteQueue(WriteQueueConfig(state_dir=tmp_path / "wq"))
    stdio = SectionStdio(world)
    applier = WriteApplier(
        queue,
        db_factory=lambda: FakeDB(world),
        reader_factory=lambda: SectionReader(world),
        stdio=stdio,
        sleep=lambda s: None,
    )
    return SimpleNamespace(queue=queue, stdio=stdio, applier=applier)


def _doc(world):
    return world.add("Doc", body="# Plan\nalpha\nbeta\n# Notes\ngamma")


def sec_args(**over):
    args = {"name": "Doc", "operation": "replace", "heading": "Plan", "body": "new"}
    args.update(over)
    return args


def test_accept_section_edit_stores_section_fingerprint(senv, world):
    _doc(world)
    out = senv.applier.accept("edit_note_section", sec_args())
    rec = senv.queue.get(out["write_id"])
    assert rec["operation"] == "edit_note_section"
    assert rec["section_fingerprint"] and len(rec["section_fingerprint"]) == 64


def test_accept_section_delete_operation_name(senv, world):
    _doc(world)
    out = senv.applier.accept(
        "edit_note_section", {"name": "Doc", "operation": "delete", "heading": "Plan"}
    )
    assert senv.queue.get(out["write_id"])["operation"] == "section_delete"


def test_accept_section_delete_over_cap_refused(senv, world):
    world.add("Big", body="# H\n" + "\n".join(f"l{i}" for i in range(51)))
    args = {"name": "Big", "operation": "delete", "heading": "H"}
    with pytest.raises(
        WriteRefused,
        match="Remote section delete is limited to 50 paragraphs; this section has 51.",
    ):
        senv.applier.accept("edit_note_section", args)


def test_accept_section_unresolvable_refused(senv, world):
    _doc(world)
    with pytest.raises(WriteRefused, match="not found"):
        senv.applier.accept("edit_note_section", sec_args(heading="Nope"))
    with pytest.raises(WriteRefused, match="exactly one of heading or section_text"):
        senv.applier.accept("edit_note_section", sec_args(section_text="alpha"))


def test_apply_section_edit_ignores_changes_elsewhere(senv, world):
    note = _doc(world)
    wid = senv.applier.accept("edit_note_section", sec_args())["write_id"]
    world.touch(note, note.text + "\nmore elsewhere")  # whole-note fingerprint moves
    assert senv.applier.apply(wid) == "applied"
    assert [c[0] for c in senv.stdio.calls] == ["edit_note_section"]
    assert senv.stdio.calls[0][2:] == ("replace", "Plan", None, "new")


def test_apply_section_find_replace_skips_whole_note_guard(senv, world):
    world.add("Doc", body="# Plan\nalpha alpha\n# Notes\nalpha")
    args = sec_args(operation="find_replace", old_text="alpha", new_text="z")
    del args["body"]
    wid = senv.applier.accept("edit_note_section", args)["write_id"]
    assert senv.applier.apply(wid) == "applied"  # 'alpha' appears 3x in the note


def test_apply_section_edit_conflict_when_section_changed(senv, world):
    note = _doc(world)
    wid = senv.applier.accept("edit_note_section", sec_args())["write_id"]
    world.touch(note, note.text.replace("alpha", "ALPHA"))
    assert senv.applier.apply(wid) == "conflict"
    assert "the section changed since the write was queued" in senv.queue.get(wid)["reason"]
    assert senv.stdio.calls == []


def test_apply_section_edit_conflict_when_section_gone(senv, world):
    note = _doc(world)
    wid = senv.applier.accept("edit_note_section", sec_args())["write_id"]
    world.touch(note, note.text.replace("# Plan", "# Other"))
    assert senv.applier.apply(wid) == "conflict"


def test_apply_section_edit_journals_prior_and_post(senv, world):
    _doc(world)
    wid = senv.applier.accept("edit_note_section", sec_args())["write_id"]
    assert senv.applier.apply(wid) == "applied"
    rec = senv.queue.get(wid)
    assert rec["prior_markdown"] == "alpha\nbeta"
    assert rec["prior_has_attachments"] is False
    assert rec["post_section_text"] == "new"
    assert rec["post_section_fingerprint"]


def test_apply_section_text_append_journals_whole_post_section(senv, world):
    _doc(world)
    args = {"name": "Doc", "operation": "append", "section_text": "alpha\nbeta", "body": "gamma2"}
    wid = senv.applier.accept("edit_note_section", args)["write_id"]
    assert senv.applier.apply(wid) == "applied"
    assert senv.queue.get(wid)["post_section_text"] == "alpha\nbeta\ngamma2"


def test_apply_section_delete_over_cap_conflicts(senv, world, monkeypatch):
    world.add("Big", body="# H\nx")
    args = {"name": "Big", "operation": "delete", "heading": "H"}
    wid = senv.applier.accept("edit_note_section", args)["write_id"]
    monkeypatch.setattr(_wa, "MAX_REMOTE_SECTION_DELETE", 0)  # re-checked at apply
    assert senv.applier.apply(wid) == "conflict"
    assert "limited to" in senv.queue.get(wid)["reason"]


def test_undo_section_replace_restores_prior(senv, world):
    note = _doc(world)
    wid = senv.applier.accept("edit_note_section", sec_args())["write_id"]
    senv.applier.apply(wid)
    assert senv.applier.undo(wid) == "undone"
    assert senv.stdio.calls[-1] == (
        "edit_note_section", "Doc", "replace", "Plan", None, "alpha\nbeta",
    )
    assert note.text == "Doc\n# Plan\nalpha\nbeta\n# Notes\ngamma"


def test_undo_by_text_uses_post_section_text(senv, world):
    _doc(world)
    args = {"name": "Doc", "operation": "replace", "section_text": "alpha\nbeta", "body": "new"}
    wid = senv.applier.accept("edit_note_section", args)["write_id"]
    senv.applier.apply(wid)
    assert senv.applier.undo(wid) == "undone"
    assert senv.stdio.calls[-1] == (
        "edit_note_section", "Doc", "replace", None, "new", "alpha\nbeta",
    )


def test_undo_section_delete_refused(senv, world):
    _doc(world)
    args = {"name": "Doc", "operation": "delete", "heading": "Plan"}
    wid = senv.applier.accept("edit_note_section", args)["write_id"]
    assert senv.applier.apply(wid) == "applied"
    with pytest.raises(
        WriteRefused,
        match="Section deletes can't be undone remotely; restore from the pre-write backup.",
    ):
        senv.applier.undo(wid)


def test_undo_section_with_table_refused(senv, world):
    note = _doc(world)
    note.table_paras = {2}
    wid = senv.applier.accept("edit_note_section", sec_args())["write_id"]
    senv.applier.apply(wid)
    assert senv.queue.get(wid)["prior_has_attachments"] is True
    note.table_paras = set()
    with pytest.raises(WriteRefused, match="attachments"):
        senv.applier.undo(wid)


def test_undo_section_with_image_refused(senv, world):
    note = _doc(world)
    note.para_atts = {2: [_sections.AttachmentRef("A1", "public.jpeg", None, None, None)]}
    wid = senv.applier.accept("edit_note_section", sec_args())["write_id"]
    senv.applier.apply(wid)
    assert senv.queue.get(wid)["prior_has_attachments"] is True


def test_section_link_and_tag_attachments_do_not_block_undo(senv, world):
    note = _doc(world)
    note.para_atts = {
        2: [
            _sections.AttachmentRef(
                "A1", "com.apple.notes.inlinetextattachment.link", None, None, None
            )
        ]
    }
    wid = senv.applier.accept("edit_note_section", sec_args())["write_id"]
    senv.applier.apply(wid)
    assert senv.queue.get(wid)["prior_has_attachments"] is False


def test_undo_refused_when_section_changed_after(senv, world):
    note = _doc(world)
    wid = senv.applier.accept("edit_note_section", sec_args())["write_id"]
    senv.applier.apply(wid)
    world.touch(note, note.text.replace("new", "the owner edited this"))
    with pytest.raises(
        WriteRefused,
        match="the section changed after this write; undoing would clobber the later edits",
    ):
        senv.applier.undo(wid)


def test_undo_allowed_when_only_elsewhere_changed(senv, world):
    note = _doc(world)
    wid = senv.applier.accept("edit_note_section", sec_args())["write_id"]
    senv.applier.apply(wid)
    world.touch(note, note.text + "\ntrailing")
    assert senv.applier.undo(wid) == "undone"


def test_undo_refused_when_post_section_unknown(senv, world):
    _doc(world)
    wid = senv.applier.accept("edit_note_section", sec_args())["write_id"]
    senv.applier.apply(wid)
    senv.queue.update(wid, post_section_fingerprint=None, post_section_text=None)
    with pytest.raises(WriteRefused, match="undo by hand"):
        senv.applier.undo(wid)


def test_undo_section_failure_is_refused_not_crash(senv, world):
    _doc(world)
    wid = senv.applier.accept("edit_note_section", sec_args())["write_id"]
    senv.applier.apply(wid)
    senv.stdio.raise_next.append(ToolError("boom"))
    with pytest.raises(WriteRefused, match="undo failed: boom"):
        senv.applier.undo(wid)


def test_allowed_operations_can_disable_section_delete_only(tmp_path, world, monkeypatch):
    monkeypatch.setattr(_wa, "load_snapshot", _fake_load_snapshot)
    from apple_notes_mcp.remote.write_queue import ALL_OPERATIONS

    cfg = WriteQueueConfig(
        state_dir=tmp_path / "wq2",
        allowed_operations=ALL_OPERATIONS - {"section_delete"},
    )
    applier = WriteApplier(
        WriteQueue(cfg),
        db_factory=lambda: FakeDB(world),
        reader_factory=lambda: SectionReader(world),
        stdio=SectionStdio(world),
        sleep=lambda s: None,
    )
    _doc(world)
    assert applier.accept("edit_note_section", sec_args())["write_id"]
    with pytest.raises(WriteRefused) as e:
        applier.accept(
            "edit_note_section",
            {"name": "Doc", "operation": "delete", "heading": "Plan"},
        )
    assert e.value.cap == "allowed_operations"


def test_queue_migrates_section_text_columns(tmp_path):
    import sqlite3

    state = tmp_path / "wq"
    WriteQueue(WriteQueueConfig(state_dir=state))
    conn = sqlite3.connect(state / "queue.sqlite")
    for col in ("section_fingerprint", "post_section_fingerprint", "post_section_text"):
        conn.execute(f"ALTER TABLE writes DROP COLUMN {col}")
    conn.commit()
    conn.close()
    WriteQueue(WriteQueueConfig(state_dir=state))
    conn = sqlite3.connect(state / "queue.sqlite")
    info = {row[1]: row for row in conn.execute("PRAGMA table_info(writes)")}
    conn.close()
    for col in ("section_fingerprint", "post_section_fingerprint", "post_section_text"):
        assert col in info and info[col][2] == "TEXT" and info[col][4] is None


# ---------------------------------------- journal only a proven span (#184)


def _race(senv, edit):
    """Run the stdio edit, then apply ``edit`` to the note as the owner's concurrent change."""
    orig = senv.stdio.edit_note_section

    def racing(*a, **k):
        result = orig(*a, **k)
        edit()
        return result

    senv.stdio.edit_note_section = racing


@pytest.mark.parametrize("change", ["insert_above", "remove_above"])
def test_post_section_not_journaled_when_paragraphs_shift_above(senv, world, change):
    note = world.add("Doc", body="intro\n# Plan\nalpha\nbeta\n# Notes\ngamma")

    def edit():
        lines = note.text.split("\n")
        if change == "insert_above":
            lines.insert(1, "owner line")
        else:
            lines.remove("intro")
        note.text = "\n".join(lines)

    args = {"name": "Doc", "operation": "replace", "section_text": "alpha\nbeta", "body": "new\nnew2"}
    wid = senv.applier.accept("edit_note_section", args)["write_id"]
    _race(senv, edit)
    assert senv.applier.apply(wid) == "applied"
    rec = senv.queue.get(wid)
    assert rec["post_section_fingerprint"] is None and rec["post_section_text"] is None
    before = note.text
    with pytest.raises(WriteRefused, match="undo by hand"):
        senv.applier.undo(wid)
    assert note.text == before


def test_post_section_not_journaled_when_heading_section_ends_early(senv, world):
    _doc(world)
    wid = senv.applier.accept(
        "edit_note_section", sec_args(body="x\n# Other\ny")
    )["write_id"]
    assert senv.applier.apply(wid) == "applied"
    rec = senv.queue.get(wid)
    assert rec["post_section_fingerprint"] is None and rec["post_section_text"] is None
    with pytest.raises(WriteRefused, match="undo by hand"):
        senv.applier.undo(wid)


def test_post_section_not_journaled_when_heading_note_changed_above(senv, world):
    note = world.add("Doc", body="intro\n# Plan\nalpha\nbeta\n# Notes\ngamma")
    wid = senv.applier.accept("edit_note_section", sec_args(body="new"))["write_id"]
    _race(senv, lambda: setattr(note, "text", note.text.replace("intro\n", "intro\nben\n")))
    assert senv.applier.apply(wid) == "applied"
    assert senv.queue.get(wid)["post_section_text"] is None


@pytest.mark.parametrize("operation", ["replace", "find_replace"])
def test_accept_section_edit_over_cap_refused(senv, world, operation):
    world.add("Big", body="# H\n" + "\n".join(f"l{i}" for i in range(51)))
    args = {"name": "Big", "operation": operation, "heading": "H"}
    args.update({"body": "x"} if operation == "replace" else {"old_text": "l1", "new_text": "m"})
    with pytest.raises(
        WriteRefused,
        match=f"Remote section {operation} is limited to 50 paragraphs; this section has 51.",
    ):
        senv.applier.accept("edit_note_section", args)


def test_accept_section_append_over_cap_allowed(senv, world):
    world.add("Big", body="# H\n" + "\n".join(f"l{i}" for i in range(51)))
    args = {"name": "Big", "operation": "append", "heading": "H", "body": "x"}
    assert senv.applier.accept("edit_note_section", args)["write_id"]


@pytest.mark.parametrize("operation", ["replace", "find_replace"])
def test_apply_section_edit_over_cap_conflicts(senv, world, monkeypatch, operation):
    _doc(world)
    args = sec_args(operation=operation)
    if operation == "find_replace":
        args.pop("body")
        args.update(old_text="alpha", new_text="z")
    wid = senv.applier.accept("edit_note_section", args)["write_id"]
    monkeypatch.setattr(_wa, "MAX_REMOTE_SECTION_DELETE", 1)
    assert senv.applier.apply(wid) == "conflict"
    assert f"Remote section {operation} is limited to" in senv.queue.get(wid)["reason"]
    assert senv.stdio.calls == []


def test_accept_section_edit_over_media_cap_refused(senv, world):
    _doc(world)
    world.media = 25_000_001
    with pytest.raises(WriteRefused, match="update_note_content"):
        senv.applier.accept("edit_note_section", sec_args())


def test_apply_conflicts_when_media_grew_over_cap(senv, world):
    _doc(world)
    wid = senv.applier.accept("edit_note_section", sec_args())["write_id"]
    world.media = 25_000_001
    assert senv.applier.apply(wid) == "conflict"
    assert "update_note_content" in senv.queue.get(wid)["reason"]
    assert senv.stdio.calls == []


def test_undo_span_proof_survives_a_whole_note_paste():
    """A paste re-stamps every run and re-mints every id; _post_section proves
    the undo span with _outside, which must only see text and rendered lines."""
    def snapshot(sig, ident):
        att = _sections.AttachmentRef(ident, "uti", None, None, None)
        paras = (
            _sections.Paragraph(0, 0, "T\n", "title", 0, None, (), (sig,)),
            _sections.Paragraph(1, 2, "a\n", "body", 0, None, (att,), (sig,)),
            _sections.Paragraph(2, 4, "b", "body", 0, None, (), (sig,)),
        )
        return _sections.NoteSnapshot(1, paras, ("T", "a", "b"))

    before = snapshot('1:{"13": 1}', "OLD")
    after = snapshot('1:{"13": 2}', "NEW")
    assert WriteApplier._outside(before, 2, 3) == WriteApplier._outside(after, 2, 3)


def test_queued_section_with_a_table_survives_another_writes_repaste(senv, world):
    """Another whole-note write re-mints the table's id; the content-level
    fingerprint must not call that a conflict (design: fingerprint change)."""
    note = world.add("Doc", body="# Plan\nbefore\ntable\n# Notes\ngamma")
    note.table_paras = {3}
    table = lambda ident: _sections.AttachmentRef(  # noqa: E731
        ident, _sections.TABLE_UTI, None, None, "cells-hash"
    )
    note.para_atts = {3: (table("T1"),)}
    wid = senv.applier.accept("edit_note_section", sec_args(operation="append"))["write_id"]
    note.para_atts = {3: (table("T2"),)}
    world.touch(note)
    assert senv.applier.apply(wid) == "applied"


class BusyStdio(SectionStdio):
    fail_times = 1

    def edit_note_section(self, *a, **k):
        if self.fail_times:
            type(self).fail_times -= 1
            raise NoteBusyError("Notes hasn't saved it yet; retry")
        return super().edit_note_section(*a, **k)


@pytest.fixture
def busy(tmp_path, world, monkeypatch):
    monkeypatch.setattr(_wa, "load_snapshot", _fake_load_snapshot)
    BusyStdio.fail_times = 1
    queue = WriteQueue(WriteQueueConfig(state_dir=tmp_path / "wq"))
    applier = WriteApplier(
        queue,
        db_factory=lambda: FakeDB(world),
        reader_factory=lambda: SectionReader(world),
        stdio=BusyStdio(world),
        sleep=lambda s: None,
    )
    return SimpleNamespace(queue=queue, applier=applier)


def test_note_busy_is_retried_not_dropped(busy, world):
    _doc(world)
    wid = busy.applier.accept("edit_note_section", sec_args())["write_id"]
    busy.applier.apply(wid)
    rec = busy.queue.get(wid)
    assert rec["status"] == "queued" and rec["attempts"] == 1
    assert "will retry" in rec["reason"]
    assert busy.applier.apply(wid) == "applied"


def test_note_busy_twice_gives_up(busy, world):
    _doc(world)
    BusyStdio.fail_times = 2
    wid = busy.applier.accept("edit_note_section", sec_args())["write_id"]
    busy.applier.apply(wid)
    busy.applier.apply(wid)
    rec = busy.queue.get(wid)
    assert rec["status"] == "failed" and "gave up after a retry" in rec["reason"]


def test_undo_journaled_when_the_paste_adds_an_empty_final_paragraph(senv, world):
    """A note ending in a list gains an empty final paragraph from any paste;
    the span must still be proven, or remote undo is lost there."""
    note = _doc(world)
    original = senv.stdio.edit_note_section

    def adds_final_blank(*a, **k):
        result = original(*a, **k)
        world.touch(note, note.text + "\n")
        return result

    senv.stdio.edit_note_section = adds_final_blank
    wid = senv.applier.accept("edit_note_section", sec_args())["write_id"]
    assert senv.applier.apply(wid) == "applied"
    assert senv.queue.get(wid)["post_section_fingerprint"] is not None


def test_undo_works_for_a_heading_section_at_note_end_with_a_final_blank(senv, world):
    """The section reaches the note's end, so the empty final paragraph belongs
    to it when undo resolves the heading: the journal must agree (review of #240)."""
    note = world.add("Doc", body="# Plan\nalpha\n# Notes\ngamma")
    original = senv.stdio.edit_note_section

    def adds_final_blank(*a, **k):
        result = original(*a, **k)
        world.touch(note, note.text + "\n")
        return result

    senv.stdio.edit_note_section = adds_final_blank
    wid = senv.applier.accept(
        "edit_note_section", sec_args(heading="Notes", body="changed")
    )["write_id"]
    assert senv.applier.apply(wid) == "applied"
    senv.stdio.edit_note_section = original
    assert senv.applier.undo(wid) == "undone"


# ----------------------------- journaling failures never fail a confirmed write


class _Log:
    def __init__(self):
        self.events = []

    def event(self, name, **fields):
        self.events.append((name, fields))


def _post_read_breaks(senv, monkeypatch):
    """load_snapshot works for the claim read, raises IndexError on the post-read."""
    calls = {"n": 0}

    def flaky(db, pk, render=None):
        calls["n"] += 1
        if calls["n"] >= 2:  # (accept already ran) apply claim, then the post-read
            raise IndexError("boom")
        return _fake_load_snapshot(db, pk, render)

    monkeypatch.setattr(_wa, "load_snapshot", flaky)


def test_post_read_unexpected_error_keeps_status_applied(senv, world, monkeypatch):
    _doc(world)
    wid = senv.applier.accept("edit_note_section", sec_args())["write_id"]
    _post_read_breaks(senv, monkeypatch)
    assert senv.applier.apply(wid) == "applied"
    rec = senv.queue.get(wid)
    assert rec["post_section_fingerprint"] is None and rec["post_section_text"] is None
    with pytest.raises(WriteRefused, match="undo by hand"):
        senv.applier.undo(wid)


def test_post_read_unexpected_error_is_logged(senv, world, monkeypatch):
    _doc(world)
    senv.applier._log = log = _Log()
    wid = senv.applier.accept("edit_note_section", sec_args())["write_id"]
    _post_read_breaks(senv, monkeypatch)
    senv.applier.apply(wid)
    events = [f for _, f in log.events if f.get("action") == "journal_error"]
    assert events and events[0]["error"] == "IndexError"
    assert "boom" not in repr(log.events)


# ------------------------- R25: blank lines a section undo can't restore


@pytest.mark.parametrize("blanks, reason", [
    (1, "undone by the owner; 1 trailing blank line not restored"),
    (2, "undone by the owner; 2 trailing blank lines not restored"),
])
def test_undo_section_ending_in_blank_line_reports_unrestored_blank(
    senv, world, blanks, reason
):
    world.add("Doc", body="# Plan\nalpha\n" + "\n" * (blanks - 1) + "\n# Notes\ngamma")
    wid = senv.applier.accept("edit_note_section", sec_args())["write_id"]
    senv.applier.apply(wid)
    assert senv.queue.get(wid)["prior_markdown"] == "alpha" + "\n" * blanks
    assert senv.applier.undo(wid) == "undone"
    assert senv.queue.get(wid)["reason"] == reason


def test_undo_section_without_blank_line_reads_exactly_undone_by_ben(senv, world):
    _doc(world)
    wid = senv.applier.accept("edit_note_section", sec_args())["write_id"]
    senv.applier.apply(wid)
    senv.applier.undo(wid)
    assert senv.queue.get(wid)["reason"] == "undone by the owner"


def test_apply_waits_on_an_open_notes_dialog_without_giving_up(env, world):
    """A dialog in Notes is waited out, never failed (issue #199): the write
    stays queued with a reason, however many passes it takes."""
    from apple_notes.exceptions import NotesModalOpenError

    world.add("Daily")
    wid = _accept(env)
    env.stdio.raise_next = [
        chained(NotesModalOpenError("a dialog is open in Notes")) for _ in range(3)
    ]
    for _ in range(3):
        assert env.applier.apply(wid) == "queued"
    rec = env.queue.get(wid)
    assert rec["attempts"] == 0
    assert "dialog is open in Notes" in rec["reason"]
    assert env.applier.apply(wid) == "applied"


def test_dialog_interrupted_write_that_changed_the_note_is_applied_unverified(
    env, world
):
    from apple_notes.exceptions import NotesModalOpenError

    note = world.add("Daily")
    wid = _accept(env)

    def partial(*a, **k):
        world.touch(note)  # earlier keys landed before the dialog appeared
        raise chained(NotesModalOpenError("a dialog is open in Notes"))

    env.stdio.update_note_content = partial
    # #200: it landed in part - never retried, undoable (was "failed").
    assert env.applier.apply(wid) == "applied_unverified"
    rec = env.queue.get(wid)
    assert "changed during a failed attempt" in rec["reason"]
    assert rec["post_fingerprint"] is not None


# ------------- R26: a section write whose verify failed is never retried

from apple_notes.exceptions import (  # noqa: E402
    SectionEditDamagedError,
    SectionEditRolledBackError,
)


def _chained_tool_error(*causes):
    """ToolError raised from causes[0], itself raised from causes[1], ..."""
    exc = None
    for cause in reversed(causes):
        try:
            raise cause from exc
        except Exception as e:
            exc = e
    try:
        raise ToolError(str(exc)) from exc
    except ToolError as te:
        return te


def test_damaged_with_not_responding_cause_is_terminal(senv, world):
    _doc(world)
    wid = senv.applier.accept("edit_note_section", sec_args())["write_id"]
    senv.stdio.raise_next.append(
        _chained_tool_error(
            SectionEditDamagedError("could not undo"),
            NotesNotRespondingError("Apple Notes didn't respond in 10s"),
        )
    )
    assert senv.applier.apply(wid) == "failed"
    rec = senv.queue.get(wid)
    assert rec["attempts"] == 0 and "could not undo" in rec["reason"]
    assert len(senv.stdio.calls) == 1  # never retried


def test_rolled_back_is_terminal(senv, world):
    _doc(world)
    wid = senv.applier.accept("edit_note_section", sec_args())["write_id"]
    senv.stdio.raise_next.append(
        _chained_tool_error(
            SectionEditRolledBackError("restored"),
            NotesNotFrontmostError("Notes lost focus"),
        )
    )
    assert senv.applier.apply(wid) == "failed"
    assert senv.queue.get(wid)["attempts"] == 0
    assert len(senv.stdio.calls) == 1


# ------------------------------------------------------------ delete / move (#229, #230)
from apple_notes.models import FolderMatch, TitleMatch  # noqa: E402

TRASH = "Recently Deleted"


class TargetDB(FakeDB):
    """FakeDB plus the delete/move guard reads (find_title_matches, folders)."""

    FOLDERS = {"Notes": 2, "Other/🗄️ Archive": 4, TRASH: 1}

    def find_title_matches(self, name):
        return [
            TitleMatch(
                pk=n.pk,
                uuid=n.uuid,
                title=n.name,
                folder=n.folder,
                folder_pk=self.FOLDERS.get(n.folder),
                modified="2024-01-02T00:00:00Z",
                in_trash=n.folder == TRASH,
                locked=n.encrypted,
            )
            for n in self.w.notes.values()
            if n.name.casefold() == name.casefold()
        ]

    def get_move_folders(self):
        return [
            FolderMatch(pk=pk, name=path.split("/")[-1], path=path)
            for path, pk in self.FOLDERS.items()
            if path != TRASH
        ]


class TargetStdio(FakeStdio):
    def __init__(self, world):
        super().__init__(world)
        self.lands = True  # False: Notes hasn't persisted the change yet

    def delete_note(self, name):
        self.calls.append(("delete_note", name))
        self._maybe_raise()
        if self.lands:
            self._by_name(name).folder = TRASH
        return {"success": True}

    def move_note(self, name, folder):
        self.calls.append(("move_note", name, folder))
        self._maybe_raise()
        if self.lands:
            self._by_name(name).folder = folder
        return {"success": True}


class TargetReader(FakeReader):
    """The applier passes a real Note (by pk) for the delete preview."""

    def get_note_content_for_note(self, note):
        return super().get_note_content_for_note(self.w.notes[note.pk])


@pytest.fixture
def tenv(tmp_path, world):
    queue = WriteQueue(WriteQueueConfig(state_dir=tmp_path / "wq"))
    stdio = TargetStdio(world)
    applier = WriteApplier(
        queue,
        db_factory=lambda: TargetDB(world),
        reader_factory=lambda: TargetReader(world),
        checklist_state=checklist_state(world),
        stdio=stdio,
        sleep=lambda s: None,
    )
    return SimpleNamespace(queue=queue, stdio=stdio, applier=applier)


def test_accept_delete_returns_the_note_to_confirm(tenv, world):
    note = world.add("Old plan", body="the first line\nsecond line")
    out = tenv.applier.accept("delete_note", {"name": "Old plan"})
    assert out["note"] == {
        "title": "Old plan",
        "folder": "Notes",
        "modified": "2024-01-02T00:00:00Z",
        "preview": "the first line second line",
    }
    rec = tenv.queue.get(out["write_id"])
    assert (rec["operation"], rec["note_uuid"]) == ("delete_note", note.uuid)
    assert tenv.stdio.calls == []  # queued, not applied


@pytest.mark.parametrize(
    "setup, name, match",
    [
        (lambda w: (w.add("Twin"), w.add("twin", folder="Other/🗄️ Archive")), "Twin", "2 notes"),
        (lambda w: None, "Nope", "0 notes"),
        (lambda w: w.add("Secret", encrypted=True), "Secret", "locked"),
        (lambda w: w.add("Gone", folder=TRASH), "Gone", "already in Recently Deleted"),
        (lambda w: w.add("Groceries"), "groceries", "exact title"),
    ],
)
def test_accept_delete_refusals(tenv, world, setup, name, match):
    setup(world)
    with pytest.raises(WriteRefused, match=match):
        tenv.applier.accept("delete_note", {"name": name})
    assert tenv.queue.list(limit=5) == []


def test_apply_delete_confirms_the_note_reached_recently_deleted(tenv, world):
    note = world.add("Old plan")
    wid = tenv.applier.accept("delete_note", {"name": "Old plan"})["write_id"]
    assert tenv.applier.apply(wid) == "applied"
    assert tenv.stdio.calls == [("delete_note", "Old plan")]
    rec = tenv.queue.get(wid)
    assert rec["reason"] is None  # confirmed, not "not confirmed"
    assert note.folder == TRASH


def test_apply_delete_unconfirmed_when_notes_has_not_persisted(tenv, world):
    world.add("Old plan")
    wid = tenv.applier.accept("delete_note", {"name": "Old plan"})["write_id"]
    tenv.stdio.lands = False
    assert tenv.applier.apply(wid) == "applied"
    assert "not confirmed" in tenv.queue.get(wid)["reason"]


@pytest.mark.parametrize(
    "change, reason",
    [
        (lambda w, n: setattr(n, "folder", TRASH), "already in Recently Deleted"),
        (lambda w, n: w.add("Old plan", folder="Other/🗄️ Archive"), "share this title"),
        (lambda w, n: setattr(n, "encrypted", True), "locked"),
        (lambda w, n: (w.notes.pop(n.pk), w.add("Old plan")), "different note"),
    ],
)
def test_apply_delete_conflicts_when_the_target_changed(tenv, world, change, reason):
    note = world.add("Old plan")
    wid = tenv.applier.accept("delete_note", {"name": "Old plan"})["write_id"]
    change(world, note)
    assert tenv.applier.apply(wid) == "conflict"
    assert reason in tenv.queue.get(wid)["reason"]
    assert tenv.stdio.calls == []


def test_accept_move_resolves_and_queues_the_folder_path(tenv, world):
    world.add("Old plan")
    out = tenv.applier.accept("move_note", {"name": "Old plan", "folder": "archive"})
    assert out["note"] == {
        "title": "Old plan",
        "old_folder": "Notes",
        "new_folder": "Other/🗄️ Archive",
    }
    rec = tenv.queue.get(out["write_id"])
    assert rec["args"]["folder"] == "Other/🗄️ Archive"


@pytest.mark.parametrize(
    "setup, args, match",
    [
        (lambda w: w.add("P"), {"name": "P", "folder": "Nowhere"}, "not found"),
        (lambda w: w.add("P"), {"name": "P", "folder": TRASH}, "not found"),
        (lambda w: w.add("P"), {"name": "P", "folder": "  "}, "cannot be empty"),
        (lambda w: w.add("P"), {"name": "P", "folder": "Notes"}, "already in"),
        (lambda w: (w.add("P"), w.add("P")), {"name": "P", "folder": "Archive"}, "2 notes"),
        (lambda w: w.add("P", encrypted=True), {"name": "P", "folder": "Archive"}, "locked"),
        (lambda w: w.add("P", folder=TRASH), {"name": "P", "folder": "Archive"}, "Recently Deleted"),
        (lambda w: None, {"name": "P", "folder": "Archive"}, "0 notes"),
    ],
)
def test_accept_move_refusals(tenv, world, setup, args, match):
    setup(world)
    with pytest.raises(WriteRefused, match=match):
        tenv.applier.accept("move_note", args)


def test_apply_move_confirms_the_new_folder(tenv, world):
    note = world.add("Old plan")
    wid = tenv.applier.accept("move_note", {"name": "Old plan", "folder": "Archive"})["write_id"]
    assert tenv.applier.apply(wid) == "applied"
    assert tenv.stdio.calls == [("move_note", "Old plan", "Other/🗄️ Archive")]
    assert tenv.queue.get(wid)["reason"] is None
    assert note.folder == "Other/🗄️ Archive"


def test_apply_move_already_there_is_a_noop(tenv, world):
    note = world.add("Old plan")
    wid = tenv.applier.accept("move_note", {"name": "Old plan", "folder": "Archive"})["write_id"]
    note.folder = "Other/🗄️ Archive"  # the owner moved it himself meanwhile
    assert tenv.applier.apply(wid) == "applied"
    assert tenv.stdio.calls == []
    assert "nothing to do" in tenv.queue.get(wid)["reason"]


def test_stdio_refusal_at_apply_is_a_failure_with_its_reason(tenv, world):
    from apple_notes.exceptions import FolderNotFoundError

    world.add("Old plan")
    wid = tenv.applier.accept("move_note", {"name": "Old plan", "folder": "Archive"})["write_id"]
    tenv.stdio.raise_next = [chained(FolderNotFoundError("Folder 'x' not found"))]
    assert tenv.applier.apply(wid) == "failed"
    assert "not found" in tenv.queue.get(wid)["reason"]


@pytest.mark.parametrize(
    "tool, args, hint",
    [
        ("delete_note", {"name": "Old plan"}, "Recently Deleted"),
        ("move_note", {"name": "Old plan", "folder": "Archive"}, "move_note"),
    ],
)
def test_undo_of_delete_and_move_is_refused_with_a_manual_route(tenv, world, tool, args, hint):
    world.add("Old plan")
    wid = tenv.applier.accept(tool, args)["write_id"]
    assert tenv.applier.apply(wid) == "applied"
    with pytest.raises(WriteRefused, match=hint):
        tenv.applier.undo(wid)
    assert tenv.queue.get(wid)["status"] == "applied"


def test_apply_move_conflicts_when_the_accepted_folder_is_gone(tenv, world, monkeypatch):
    """Review (Codex): never re-resolve to a different folder at apply time."""
    world.add("Old plan")
    monkeypatch.setattr(
        TargetDB, "FOLDERS", {"Notes": 2, "Archive": 4, "Work/Archive": 7, TRASH: 1}
    )
    wid = tenv.applier.accept("move_note", {"name": "Old plan", "folder": "Archive"})["write_id"]
    assert tenv.queue.get(wid)["args"]["folder"] == "Archive"
    # The top-level Archive is renamed away; "Archive" now names only Work/Archive.
    monkeypatch.setattr(TargetDB, "FOLDERS", {"Notes": 2, "Work/Archive": 7, TRASH: 1})
    assert tenv.applier.apply(wid) == "conflict"
    assert "destination folder" in tenv.queue.get(wid)["reason"]
    assert tenv.stdio.calls == []


def test_undo_add_note_refused_cleanly_when_the_delete_guard_refuses(env, world):
    """Review: the guard's own exceptions must not crash ./run remote-writes undo."""
    wid = _accept(env, "add_note", {"name": "Fresh", "body": "b"})
    assert env.applier.apply(wid) == "applied"

    def refuse(name):
        raise EncryptedNoteError(f"Note '{name}' is password-protected (locked)")

    env.applier._delete_note = refuse
    with pytest.raises(WriteRefused, match="undo failed: .*locked"):
        env.applier.undo(wid)


def test_accept_delete_survives_an_unreadable_preview(tenv, world):
    from apple_notes.exceptions import AppleNotesError

    world.add("Old plan")

    class Broken(TargetReader):
        def get_note_content_for_note(self, note):
            raise AppleNotesError("attachment lookup failed")

    tenv.applier._reader = lambda: Broken(world)
    out = tenv.applier.accept("delete_note", {"name": "Old plan"})
    assert out["note"]["preview"] is None


@pytest.mark.parametrize(
    "over",
    [
        {"body": TWO_COL},
        {"body": "see {#plan}"},
        {"operation": "append", "body": TWO_COL},
        {"operation": "find_replace", "body": None, "old_text": "alpha", "new_text": TWO_COL},
    ],
)
def test_accept_section_edit_refuses_new_tags_and_tables(senv, world, over):
    """#264: edit_note_section's own tag/table refusal (v1 limit) is
    returned at accept too, not only when the queue applies it."""
    _doc(world)
    args = {k: v for k, v in sec_args(**over).items() if v is not None}
    with pytest.raises(WriteRefused, match="can't be added by edit_note_section"):
        senv.applier.accept("edit_note_section", args)
    assert senv.queue.list() == []


def test_accept_reports_a_missing_note_before_its_wide_table(env, world):
    """The note checks run first, as in the stdio tool: a caller told to
    split the table would only then learn the note doesn't exist."""
    with pytest.raises(WriteRefused, match="not found"):
        env.applier.accept(
            "update_note_content", _table_write("update_note_content", THREE_COL, "append")
        )
    world.add("Fresh")
    with pytest.raises(WriteRefused, match="already exists"):
        env.applier.accept("add_note", _table_write("add_note", THREE_COL))
