"""
Folder filtering utilities for Apple Notes.

Provides case-insensitive substring matching for filtering notes by folder.
"""
from typing import Protocol


class HasFolder(Protocol):
    """Protocol for objects that have a folder attribute."""
    folder: str


def matches_folder_filter(folder: str, patterns: list[str]) -> bool:
    """
    Check if a folder matches any of the given patterns.

    Uses case-insensitive substring matching. For example:
    - 'Archive' matches 'Other/Archive'
    - 'work' matches 'Work/Projects'

    Args:
        folder: The folder path to check
        patterns: List of pattern strings to match against

    Returns:
        True if folder matches any pattern
    """
    if not patterns:
        return False
    folder_lower = folder.lower()
    return any(p.lower() in folder_lower for p in patterns)


def filter_notes(
    notes: list[HasFolder],
    exclude_folders: list[str] | None = None,
    include_folders: list[str] | None = None,
) -> list[HasFolder]:
    """
    Filter notes by folder inclusion/exclusion rules.

    Applies include_folders first (whitelist), then exclude_folders (blacklist).
    This allows patterns like "include all Work folders except Work/Archive".

    Args:
        notes: List of note objects with a 'folder' attribute
        exclude_folders: Folder patterns to exclude (case-insensitive substring)
        include_folders: Only include notes matching these patterns (if set)

    Returns:
        Filtered list of notes
    """
    filtered = list(notes)

    # Apply include filter first (whitelist)
    if include_folders:
        filtered = [n for n in filtered if matches_folder_filter(n.folder, include_folders)]

    # Apply exclude filter (blacklist)
    if exclude_folders:
        filtered = [n for n in filtered if not matches_folder_filter(n.folder, exclude_folders)]

    return filtered


def filter_folders(
    folders: list[dict],
    exclude_folders: list[str] | None = None,
    include_folders: list[str] | None = None,
) -> list[dict]:
    """
    Filter folder dicts by inclusion/exclusion rules.

    Applies include_folders first (whitelist), then exclude_folders (blacklist).
    Uses the 'full_path' key from folder dicts for matching.

    Args:
        folders: List of folder dicts with 'full_path' key
        exclude_folders: Folder patterns to exclude (case-insensitive substring)
        include_folders: Only include folders matching these patterns (if set)

    Returns:
        Filtered list of folder dicts
    """
    filtered = list(folders)

    # Apply include filter first (whitelist)
    if include_folders:
        filtered = [f for f in filtered if matches_folder_filter(f["full_path"], include_folders)]

    # Apply exclude filter (blacklist)
    if exclude_folders:
        filtered = [f for f in filtered if not matches_folder_filter(f["full_path"], exclude_folders)]

    return filtered
