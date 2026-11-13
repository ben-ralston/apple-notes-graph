#!/usr/bin/env python3
"""
Extract notes and internal links from Apple Notes SQLite database.
Outputs JSON with notes and their relationships for graph visualization.

This script uses the apple_notes library for database operations.
"""

import sys
import os
import json

# Add src directory to path for library import
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from apple_notes import DatabaseNotFoundError, unified_log
from apple_notes.ui_payload import get_ui_graph_data


def extract_notes_data():
    """Extract notes, links, clusters, and health for the graph UI."""
    try:
        return get_ui_graph_data()
    except DatabaseNotFoundError as e:
        unified_log.report_failure("extract_notes", exc=e)
        return {
            "error": str(e),
            "notes": [],
            "links": [],
            "health": {
                "orphans": [],
                "broken_links": [],
                "stale": [],
                "duplicate_titles": [],
            },
            "stats": {"total_notes": 0, "total_links": 0},
        }


if __name__ == "__main__":
    unified_log.configure(component="script:extract_notes", enqueue_only=True)
    data = extract_notes_data()
    print(json.dumps(data, indent=2, ensure_ascii=False))
