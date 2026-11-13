#!/usr/bin/env python3
"""Append a [[link]] to a note - the Electron add-link IPC endpoint.

pk-addressed source (duplicate-title safe on the read side), but the UI
scripting layer targets notes BY NAME - so a duplicated source title is
refused outright, and a missing target is refused because the [[link]]
would silently degrade to plain text.
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from apple_notes import unified_log


def main() -> None:
    unified_log.configure(component="script:add_link", enqueue_only=True)
    parser = argparse.ArgumentParser()
    parser.add_argument("--pk", type=int, required=False)
    parser.add_argument("--target", required=False)
    args = parser.parse_args()

    from apple_notes import demo_mode

    if demo_mode.is_demo():
        print(json.dumps({"error": demo_mode.refusal("Adding a link")}))
        return

    if args.pk is None or not args.target:
        print(json.dumps({"error": "--pk and --target are required"}))
        return

    if "]]" in args.target or "[[" in args.target:
        # Would break the [[...]] wiki-link markdown the write path builds.
        print(json.dumps({"error": "ValueError: invalid target title ('[[' / ']]')"}))
        return

    try:
        from apple_notes.database import NotesDatabase
        from apple_notes.write_service import append_markdown_to_note

        with NotesDatabase() as db:
            notes = db.get_all_notes()

        source = next((n for n in notes if n.pk == args.pk), None)
        if source is None:
            print(
                json.dumps({"error": f"NoteNotFoundError: no note with pk {args.pk}"})
            )
            return
        if sum(1 for n in notes if n.name.lower() == source.name.lower()) > 1:
            print(
                json.dumps(
                    {
                        "error": "DuplicateTitleError: source title is not unique - "
                        "add the link in Apple Notes directly"
                    }
                )
            )
            return
        targets = [n for n in notes if n.name.lower() == args.target.lower()]
        if not targets:
            print(
                json.dumps(
                    {"error": f"NoteNotFoundError: target '{args.target}' not found"}
                )
            )
            return
        if len(targets) > 1:
            # Link targets resolve by title (lower-cased), so an ambiguous
            # target could silently link the wrong note.
            print(
                json.dumps(
                    {
                        "error": "DuplicateTitleError: target title is not unique - "
                        "add the link in Apple Notes directly"
                    }
                )
            )
            return
        target = targets[0]

        result = append_markdown_to_note(source.name, f"[[{target.name}]]")
        print(json.dumps({"ok": True, **result}))
    except Exception as e:  # UI consumes errors as data; never crash the IPC call
        unified_log.report_failure("add_link", exc=e)
        print(json.dumps({"error": f"{type(e).__name__}: {e}"}))


if __name__ == "__main__":
    main()
