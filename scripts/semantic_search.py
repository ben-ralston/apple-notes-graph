#!/usr/bin/env python3
"""Semantic search over the embedding cache for the Electron ⌘K palette."""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from apple_notes import unified_log


def main() -> None:
    unified_log.configure(component="script:semantic_search", enqueue_only=True)
    parser = argparse.ArgumentParser()
    parser.add_argument("--query", required=False)
    parser.add_argument("--top-k", type=int, default=8)
    args = parser.parse_args()

    if not args.query:
        print(json.dumps({"error": "--query is required"}))
        return

    try:
        from apple_notes.embeddings import semantic_search

        print(
            json.dumps(
                semantic_search(args.query, top_k=args.top_k), ensure_ascii=False
            )
        )
    except Exception as e:  # UI consumes errors as data; never crash the IPC call
        unified_log.report_failure("semantic_search", exc=e)
        print(json.dumps({"error": f"{type(e).__name__}: {e}"}))


if __name__ == "__main__":
    main()
