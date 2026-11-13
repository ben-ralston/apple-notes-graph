#!/usr/bin/env python3
"""Link-suggestion recall for the Electron inspector (candidates + reasons).

No folder filters: like extract_notes.py, filtering is the UI's job.
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from apple_notes import unified_log


def main() -> None:
    unified_log.configure(component="script:suggest_links", enqueue_only=True)
    parser = argparse.ArgumentParser()
    parser.add_argument("--pk", type=int, required=False)
    parser.add_argument("--max-candidates", type=int, default=25)
    args = parser.parse_args()

    if args.pk is None:
        print(json.dumps({"error": "--pk is required"}))
        return

    try:
        from apple_notes.librarian import get_link_suggestions

        result = get_link_suggestions(
            note_pk=args.pk, max_candidates=args.max_candidates
        )
        print(json.dumps(result, ensure_ascii=False))
    except Exception as e:  # UI consumes errors as data; never crash the IPC call
        unified_log.report_failure("suggest_links", exc=e)
        print(json.dumps({"error": f"{type(e).__name__}: {e}"}))


if __name__ == "__main__":
    main()
