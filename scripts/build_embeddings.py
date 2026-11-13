#!/usr/bin/env python3
"""Build/refresh the semantic-search embedding cache (background, incremental)."""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from apple_notes import unified_log


def main() -> None:
    unified_log.configure(component="script:build_embeddings", enqueue_only=True)
    try:
        from apple_notes.embeddings import build_embeddings

        print(json.dumps(build_embeddings()))
    except Exception as e:  # background job - errors are data, main.js just logs
        unified_log.report_failure("build_embeddings", exc=e)
        print(json.dumps({"error": f"{type(e).__name__}: {e}"}))


if __name__ == "__main__":
    main()
