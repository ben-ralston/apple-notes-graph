#!/usr/bin/env python3
"""Delete a note (move to Recently Deleted) - the Electron delete-note IPC
endpoint.

pk-addressed on the read side, but the AppleScript delete targets notes BY
NAME - so a duplicated title is refused outright (it could delete the wrong
note). The write chain (backup -> local/daemon route -> sync nudge) is
write_service.delete_note_by_name, shared with the MCP delete_note tool.
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from apple_notes import unified_log


def main() -> None:
    unified_log.configure(component="script:delete_note", enqueue_only=True)
    parser = argparse.ArgumentParser()
    # Parsed as str so a malformed pk stays inside the JSON error contract
    # (argparse type=int would exit 2 with a usage message instead).
    parser.add_argument("--pk", required=False)
    args = parser.parse_args()

    from apple_notes import demo_mode

    if demo_mode.is_demo():
        print(json.dumps({"error": demo_mode.refusal("Deleting a note")}))
        return

    try:
        pk = int(args.pk)
    except (TypeError, ValueError):
        print(json.dumps({"error": "--pk is required and must be an integer"}))
        return

    try:
        from apple_notes.database import NotesDatabase
        from apple_notes.write_service import delete_note_by_name

        with NotesDatabase() as db:
            notes = db.get_all_notes()

        note = next((n for n in notes if n.pk == pk), None)
        if note is None:
            print(json.dumps({"error": f"NoteNotFoundError: no note with pk {pk}"}))
            return
        if sum(1 for n in notes if n.name.lower() == note.name.lower()) > 1:
            print(
                json.dumps(
                    {
                        "error": "DuplicateTitleError: note title is not unique - "
                        "delete it in Apple Notes directly"
                    }
                )
            )
            return

        result = delete_note_by_name(note.name)
        print(json.dumps({"ok": True, "name": note.name, **result}))
    except Exception as e:  # UI consumes errors as data; never crash the IPC call
        unified_log.report_failure("delete_note", exc=e)
        print(json.dumps({"error": f"{type(e).__name__}: {e}"}))


if __name__ == "__main__":
    main()
