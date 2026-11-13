"""Tests for the UI graph payload (extract_notes.py v2)."""

from datetime import datetime, timezone

from apple_notes.librarian import assign_cluster_ids
from apple_notes.models import GraphLink, Note
from apple_notes.ui_payload import build_ui_payload


def _note(
    pk,
    name,
    folder="Notes",
    modified="2026-01-01T00:00:00Z",
    created=None,
    last_opened=None,
):
    return Note(
        name=name,
        uuid=f"UUID-{pk}",
        folder=folder,
        pk=pk,
        modified=modified,
        created=created,
        last_opened=last_opened,
    )


def _link(source, target):
    return GraphLink(
        source_pk=source.pk,
        target_pk=target.pk,
        source_name=source.name,
        target_name=target.name,
    )


class TestAssignClusterIds:
    def test_two_components_ordered_by_size(self):
        a, b, c, d, e = (_note(i, f"N{i}") for i in range(1, 6))
        links = [_link(a, b), _link(b, c), _link(d, e)]
        ids = assign_cluster_ids([a, b, c, d, e], links)
        assert ids[a.pk] == ids[b.pk] == ids[c.pk] == 0  # bigger component first
        assert ids[d.pk] == ids[e.pk] == 1

    def test_unlinked_note_gets_minus_one(self):
        a, b, lone = _note(1, "A"), _note(2, "B"), _note(3, "Lone")
        ids = assign_cluster_ids([a, b, lone], [_link(a, b)])
        assert ids[lone.pk] == -1

    def test_self_link_does_not_cluster(self):
        a = _note(1, "A")
        ids = assign_cluster_ids([a], [_link(a, a)])
        assert ids[a.pk] == -1


class TestBuildUiPayload:
    def test_payload_shape(self):
        a = _note(
            1,
            "★ Index",
            created="2025-11-20T00:00:00Z",
            last_opened="2026-01-02T00:00:00Z",
        )
        b, lone = _note(2, "Daily Briefing"), _note(3, "Lone")
        payload = build_ui_payload(
            notes=[a, b, lone],
            links=[_link(a, b)],
            broken=[],
            stale_threshold_days=180,
            now=datetime(2026, 7, 11, tzinfo=timezone.utc),
        )
        assert {n["id"] for n in payload["notes"]} == {1, 2, 3}
        home = next(n for n in payload["notes"] if n["id"] == 1)
        assert home == {
            "id": 1,
            "name": "★ Index",
            "folder": "Notes",
            "created": "2025-11-20T00:00:00Z",
            "modified": "2026-01-01T00:00:00Z",
            "last_opened": "2026-01-02T00:00:00Z",
            "connections": 1,
            "cluster": 0,
        }
        briefing = next(n for n in payload["notes"] if n["id"] == 2)
        assert briefing["created"] is None
        assert briefing["last_opened"] is None
        assert payload["links"] == [{"source": 1, "target": 2}]
        assert payload["health"]["orphans"] == [3]
        assert payload["health"]["stale"] == [1, 2, 3]  # all older than cutoff
        assert payload["stats"] == {"total_notes": 3, "total_links": 1}

    def test_broken_and_duplicates_included(self):
        from apple_notes.models import BrokenLink

        a = _note(1, "Recipe")
        b = _note(2, "recipe")
        broken = [
            BrokenLink(
                source_name="Recipe",
                source_folder="Notes",
                target_uuid="X",
                intended_target_title="Gone",
                source_pk=1,
            )
        ]
        payload = build_ui_payload(
            notes=[a, b],
            links=[],
            broken=broken,
            stale_threshold_days=180,
            now=datetime(2026, 7, 11, tzinfo=timezone.utc),
        )
        assert payload["health"]["broken_links"] == [
            {
                "source_pk": 1,
                "source_name": "Recipe",
                "intended_target_title": "Gone",
                "target_uuid": "X",
                "occurrences": 1,
            }
        ]
        assert payload["health"]["duplicate_titles"] == [{"name": "recipe", "count": 2}]
