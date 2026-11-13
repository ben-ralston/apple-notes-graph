"""Re-point an existing note link: target resolution and guards (issue #183).

Shared by the stdio ``replace_note_link`` tool and the remote write queue
(``remote/write_apply.py``) so the two can never drift. Everything here reads
NoteStore only; the write itself is ``ui_operations.execute_replace_note_link``.

A link matches a target when it is a note-link attachment whose token uuid
equals the target uuid (case-insensitive). The guard (``plan_relink``), the
engine (``richtext.relink``) and verification (``await_relinked``) all use
that one predicate.
"""

import re
import time
from dataclasses import dataclass
from typing import Callable, Optional


_UUID_RE = re.compile(
    r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$"
)


class RelinkError(ValueError):
    """A relink was refused; the message is for the caller."""


@dataclass(frozen=True)
class RelinkPlan:
    old_uuid: str
    new_uuid: str
    new_title: str
    count: int  # links to be re-pointed
    prior_new_count: int  # links already pointing at new_uuid


def is_uuid(value: str) -> bool:
    """Whether a target argument is a note uuid (else it is a title)."""
    return bool(_UUID_RE.match(value or ""))


def _uuid_list(uuids) -> str:
    return ", ".join(sorted(uuids))


def plan_relink(
    db, note, old_target: str, new_target: str, replace_all: bool
) -> RelinkPlan:
    """Resolve both targets against the source note and check the guards.

    ``note`` is the source Note; the caller has already made sure its title is
    unique (UI scripting opens notes by title).

    - old_target as a uuid matches the note's raw link targets directly, so a
      link whose target note was deleted can still be re-pointed.
    - old_target as a title matches links whose target is a live note with
      that title (case-insensitive); links to two different such notes are
      ambiguous.
    - new_target must be a live note (not trashed, visible in folder scope),
      unique by title when given as one.

    Raises:
        RelinkError: on any refusal, before anything is written
    """
    targets = db.get_note_link_targets(note.pk)
    live = db.build_uuid_lookup()

    if is_uuid(old_target):
        matched = {old_target.upper()} & set(targets)
    else:
        wanted = old_target.casefold()
        matched = {
            t for t in targets if t in live and live[t].name.casefold() == wanted
        }
        if len(matched) > 1:
            raise RelinkError(
                f"Note '{note.name}' links to {len(matched)} different notes titled "
                f"'{old_target}' ({_uuid_list(matched)}); pass old_target as one of these uuids."
            )
    if not matched:
        raise RelinkError(f"No link to '{old_target}' in note '{note.name}'.")
    old_uuid = next(iter(matched))
    count = targets.count(old_uuid)
    if count > 1 and not replace_all:
        raise RelinkError(
            f"Found {count} links to '{old_target}' in note '{note.name}'; "
            "pass replace_all=true to re-point all of them."
        )

    if is_uuid(new_target):
        new_note = live.get(new_target.upper())
        if new_note is None:
            raise RelinkError(
                f"New target note {new_target} not found (it may be deleted or in "
                "Recently Deleted)."
            )
    else:
        wanted = new_target.casefold()
        candidates = [n for n in db.get_all_notes() if n.name.casefold() == wanted]
        if not candidates:
            raise RelinkError(
                f"New target note '{new_target}' not found. Use list_notes to see available notes."
            )
        if len(candidates) > 1:
            raise RelinkError(
                f"{len(candidates)} notes are titled '{new_target}' "
                f"({_uuid_list(n.uuid.upper() for n in candidates)}); pass new_target as one of "
                "these uuids."
            )
        new_note = candidates[0]

    new_uuid = new_note.uuid.upper()
    if new_uuid == old_uuid:
        raise RelinkError(f"The link already points at '{new_note.name}'.")
    return RelinkPlan(
        old_uuid=old_uuid,
        new_uuid=new_uuid,
        new_title=new_note.name,
        count=count,
        prior_new_count=targets.count(new_uuid),
    )


def await_relinked(
    db_factory: Callable,
    pk: int,
    plan: RelinkPlan,
    *,
    before_fingerprint: Optional[str],
    tries: int = 20,
    interval: float = 0.25,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Poll NoteStore until it shows the relink; True once confirmed.

    Confirmed = the note changed, no link is left to old_uuid, and the links
    to new_uuid rose by plan.count. Anything else within the window is
    "applied, not confirmed" (False), never an error: Notes persists lazily
    and may save an unrelated edit first, while the engine has already
    checked the paste in the editor itself.
    """
    want_new = plan.prior_new_count + plan.count
    for attempt in range(tries):
        with db_factory() as db:
            if db.note_fingerprint(pk) != before_fingerprint:
                targets = db.get_note_link_targets(pk)
                if plan.old_uuid not in targets and targets.count(plan.new_uuid) >= want_new:
                    return True
        if attempt < tries - 1:
            sleep(interval)
    return False
