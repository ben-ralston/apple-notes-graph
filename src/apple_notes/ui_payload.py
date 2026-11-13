"""Assemble the Electron UI graph payload (extract_notes.py v2).

Pure assembly over librarian primitives: build_ui_payload does no I/O;
get_ui_graph_data owns database access.
"""

from datetime import datetime, timezone
from typing import List, Optional

from .librarian import (
    _fetch_filtered_graph,
    assign_cluster_ids,
    compute_connection_counts,
    find_duplicate_names,
    find_orphan_notes,
    find_stale_notes,
)
from .models import BrokenLink, GraphLink, Note

STALE_THRESHOLD_DAYS = 180


def build_ui_payload(
    notes: List[Note],
    links: List[GraphLink],
    broken: List[BrokenLink],
    stale_threshold_days: int,
    now: datetime,
) -> dict:
    """Assemble the UI payload from pre-fetched graph data."""
    counts = compute_connection_counts(notes, links)
    clusters = assign_cluster_ids(notes, links)
    orphans = find_orphan_notes(notes, links)
    stale = find_stale_notes(notes, stale_threshold_days, now)

    return {
        "notes": [
            {
                "id": n.pk,
                "name": n.name,
                "folder": n.folder,
                "created": n.created,
                "modified": n.modified,
                "last_opened": n.last_opened,
                "connections": counts[n.pk],
                "cluster": clusters[n.pk],
            }
            for n in notes
        ],
        "links": [{"source": l.source_pk, "target": l.target_pk} for l in links],
        "health": {
            "orphans": [n.pk for n in orphans],
            "broken_links": [
                {
                    "source_pk": b.source_pk,
                    "source_name": b.source_name,
                    "intended_target_title": b.intended_target_title,
                    "target_uuid": b.target_uuid,
                    "occurrences": b.occurrences,
                }
                for b in broken
            ],
            "stale": [n.pk for n in stale],
            "duplicate_titles": [
                {"name": d["name"], "count": d["count"]}
                for d in find_duplicate_names(notes)
            ],
        },
        "stats": {"total_notes": len(notes), "total_links": len(links)},
    }


def get_ui_graph_data(
    db_path: Optional[str] = None,
    stale_threshold_days: int = STALE_THRESHOLD_DAYS,
) -> dict:
    """Fetch the full (unfiltered) graph and assemble the UI payload.

    Folder filtering stays client-side so filters are interactive and
    cluster colors stay stable across filter toggles.
    """
    notes, links, broken = _fetch_filtered_graph(db_path, None, None)
    return build_ui_payload(
        notes, links, broken, stale_threshold_days, datetime.now(timezone.utc)
    )
