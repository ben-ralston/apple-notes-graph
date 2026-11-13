#!/usr/bin/env python3
"""Read one note's content as JSON for the Electron inspector preview."""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from apple_notes import unified_log


def main() -> None:
    unified_log.configure(component="script:read_note", enqueue_only=True)
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", required=False)
    parser.add_argument("--pk", type=int, required=False)
    args = parser.parse_args()

    if bool(args.name) == bool(args.pk is not None):
        print(json.dumps({"error": "exactly one of --name or --pk is required"}))
        return

    try:
        if args.pk is not None:
            from apple_notes.database import NotesDatabase
            from apple_notes.note_content import NoteContentReader

            with NotesDatabase() as db:
                note = db.get_note_by_pk(args.pk)
            content = NoteContentReader().get_note_content_for_note(note)
        else:
            from apple_notes import read_note

            content = read_note(args.name)

        print(
            json.dumps(
                {
                    "name": content.name,
                    "folder": content.folder,
                    "modified": content.modified,
                    "content": content.resolved_text,
                },
                ensure_ascii=False,
            )
        )
    except Exception as e:  # UI consumes errors as data; never crash the IPC call
        unified_log.report_failure("read_note", exc=e)
        print(json.dumps({"error": f"{type(e).__name__}: {e}"}))


if __name__ == "__main__":
    main()
