#!/usr/bin/env python3
"""
Read Apple Notes content with links resolved to [[Note Name]] format.

Usage:
    python scripts/read_note_content.py "Note Title"
    python scripts/read_note_content.py --list  # List all notes

This script demonstrates what note content looks like when internal links,
external URLs, and checklists are properly resolved.
"""
import sys
import os

# Add src directory to path for library import
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from apple_notes import (
    read_note,
    NoteContentReader,
    NoteNotFoundError,
    DatabaseNotFoundError,
)


def list_all_notes():
    """List all notes with their folders."""
    try:
        reader = NoteContentReader()
        notes = reader.get_all_notes()
    except DatabaseNotFoundError as e:
        print(f"Error: {e}")
        sys.exit(1)

    print("All Notes:")
    print("=" * 60)

    # Group by folder
    by_folder = {}
    for note in notes:
        folder = note.folder or "Notes"
        if folder not in by_folder:
            by_folder[folder] = []
        by_folder[folder].append(note.name)

    for folder in sorted(by_folder.keys()):
        print(f"\n[{folder}]")
        for name in sorted(by_folder[folder]):
            print(f"  - {name}")


def main():
    if len(sys.argv) < 2:
        print("Usage: python scripts/read_note_content.py 'Note Title'")
        print("       python scripts/read_note_content.py --list")
        sys.exit(1)

    if sys.argv[1] == '--list':
        list_all_notes()
        return

    note_title = sys.argv[1]

    print(f"Reading note: {note_title}")
    print("=" * 60)

    try:
        content = read_note(note_title)
        print(content.resolved_text)
    except NoteNotFoundError:
        print(f"Error: Note '{note_title}' not found")
        sys.exit(1)
    except DatabaseNotFoundError as e:
        print(f"Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
